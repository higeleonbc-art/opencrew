"""Riot Games公式素材自動ダウンロードモジュール

Data Dragon (ddragon) APIを使用して、チャンピオンのスプラッシュアート・
アイコンを自動取得する。

Data Dragon: https://developer.riotgames.com/docs/lol#data-dragon
- 公式の静的データ配信CDN
- APIキー不要（公開リソース）
- スプラッシュアートとアイコンを取得可能

【サーバー保護ポリシー】
- Data DragonはRiotの公式CDNで静的ファイル配信のため負荷は低い
- それでもリクエスト間隔を確保（1秒）
- 1セッションあたりのDL上限を設定（200ファイル）
- ファイルサイズ上限（20MB）
- エラー時のリトライなし
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from .script_analyzer import CHAMPION_NAME_MAP


# Data Dragon ベースURL
DDRAGON_BASE = "https://ddragon.leagueoflegends.com"

# --- サーバー保護パラメータ ---
_REQUEST_DELAY = 1.0                    # リクエスト間隔（秒）
_MAX_DOWNLOADS_PER_SESSION = 200        # 1セッションのDL上限
_MAX_FILE_SIZE = 20 * 1024 * 1024       # 20MB（スプラッシュアートは大きめ）
_REQUEST_TIMEOUT = 30


@dataclass
class DownloadResult:
    """ダウンロード結果"""
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def get_latest_version() -> str:
    """Data Dragonの最新バージョンを取得"""
    url = f"{DDRAGON_BASE}/api/versions.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    versions = resp.json()
    return versions[0]


def get_champion_data(version: str) -> dict:
    """全チャンピオンのメタデータを取得

    Returns:
        {"Ahri": {"id": "Ahri", "key": "103", "name": "Ahri", ...}, ...}
    """
    url = f"{DDRAGON_BASE}/cdn/{version}/data/ja_JP/champion.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"]


def get_champion_detail(version: str, champion_id: str) -> dict:
    """チャンピオン個別の詳細データ（スキン一覧を含む）を取得"""
    url = f"{DDRAGON_BASE}/cdn/{version}/data/ja_JP/champion/{champion_id}.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()["data"][champion_id]


def _validate_ddragon_url(url: str) -> bool:
    """URLがData Dragonドメインか検証"""
    try:
        parsed = urlparse(url)
        return parsed.hostname == "ddragon.leagueoflegends.com"
    except Exception:
        return False


def _download_file(url: str, dest: Path, overwrite: bool = False) -> bool:
    """ファイルをダウンロード（既存ファイルはスキップ、サイズ制限付き）"""
    if dest.exists() and not overwrite:
        return False

    if not _validate_ddragon_url(url):
        print(f"    [拒否] 許可されていないドメイン: {url}")
        return False

    resp = requests.get(url, timeout=_REQUEST_TIMEOUT, stream=True)
    resp.raise_for_status()

    # サイズチェック
    content_length = resp.headers.get("Content-Length")
    if content_length and int(content_length) > _MAX_FILE_SIZE:
        resp.close()
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    total_size = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            total_size += len(chunk)
            if total_size > _MAX_FILE_SIZE:
                # サイズ超過 → 途中ファイルを削除
                f.close()
                dest.unlink(missing_ok=True)
                return False
            f.write(chunk)
    return True


def _get_champion_id_for_name(en_name: str, champion_data: dict) -> str | None:
    """英語名からData Dragon上のchampion IDを解決

    CHAMPION_NAME_MAPでは "LeeSin" のようにスペース無しで格納しているが、
    Data DragonのIDも同様の形式なので基本はそのまま使える。
    """
    # 直接一致
    if en_name in champion_data:
        return en_name

    # 大文字小文字を無視して探索
    lower_map = {k.lower(): k for k in champion_data}
    if en_name.lower() in lower_map:
        return lower_map[en_name.lower()]

    return None


class RiotAssetDownloader:
    """Riot Data Dragonからの素材ダウンローダー"""

    def __init__(self, asset_dirs: dict[str, str]):
        """
        Args:
            asset_dirs: {"splash": "/path/to/splash", "icons": "/path/to/icons", ...}
        """
        self.splash_dir = Path(asset_dirs.get("splash", "./opencrew_assets/splash"))
        self.icons_dir = Path(asset_dirs.get("icons", "./opencrew_assets/icons"))
        self._version: str | None = None
        self._champion_data: dict | None = None
        self._session_downloads = 0  # セッション内DLカウンタ

    def _ensure_data(self) -> None:
        """バージョン・チャンピオンデータを取得（キャッシュ）"""
        if self._version is None:
            print("  Data Dragonバージョンを取得中...")
            self._version = get_latest_version()
            print(f"  バージョン: {self._version}")
        if self._champion_data is None:
            print("  チャンピオンデータを取得中...")
            self._champion_data = get_champion_data(self._version)
            print(f"  チャンピオン数: {len(self._champion_data)}")

    def download_champion_splash(
        self,
        champion_en: str,
        max_skins: int = 5,
        overwrite: bool = False,
    ) -> DownloadResult:
        """チャンピオンのスプラッシュアートをダウンロード

        Data Dragonの命名: {champion_id}_{skin_num}.jpg
        保存先の命名: {champion_en}_{連番}.jpg

        Args:
            champion_en: 英語チャンピオン名（CHAMPION_NAME_MAPの値）
            max_skins: ダウンロードするスキン数上限（0=デフォルトのみ）
            overwrite: 既存ファイルを上書きするか
        """
        result = DownloadResult()
        self._ensure_data()

        champ_id = _get_champion_id_for_name(champion_en, self._champion_data)
        if not champ_id:
            result.errors.append(f"チャンピオンID不明: {champion_en}")
            return result

        # 詳細データからスキン一覧取得
        try:
            detail = get_champion_detail(self._version, champ_id)
        except Exception as e:
            result.errors.append(f"詳細データ取得失敗 ({champion_en}): {e}")
            return result

        skins = detail.get("skins", [])
        # skin_num=0 がデフォルト
        skins_to_download = skins[: max_skins + 1] if max_skins > 0 else skins[:1]

        for i, skin in enumerate(skins_to_download):
            # セッション上限チェック
            if self._session_downloads >= _MAX_DOWNLOADS_PER_SESSION:
                result.errors.append(
                    f"セッションDL上限到達 ({_MAX_DOWNLOADS_PER_SESSION})")
                break

            skin_num = skin["num"]
            url = (
                f"{DDRAGON_BASE}/cdn/img/champion/splash/"
                f"{champ_id}_{skin_num}.jpg"
            )
            dest = self.splash_dir / f"{champion_en}_{i}.jpg"

            try:
                downloaded = _download_file(url, dest, overwrite=overwrite)
                if downloaded:
                    result.downloaded.append(str(dest))
                    self._session_downloads += 1
                else:
                    result.skipped.append(str(dest))
                time.sleep(_REQUEST_DELAY)
            except Exception as e:
                self._session_downloads += 1  # 失敗もカウント
                result.failed.append(str(dest))
                result.errors.append(f"DL失敗 ({dest.name}): {e}")

        return result

    def download_champion_icon(
        self,
        champion_en: str,
        overwrite: bool = False,
    ) -> DownloadResult:
        """チャンピオンアイコン（正方形）をダウンロード

        保存先: icons/{champion_en}_0.png
        """
        result = DownloadResult()
        self._ensure_data()

        champ_id = _get_champion_id_for_name(champion_en, self._champion_data)
        if not champ_id:
            result.errors.append(f"チャンピオンID不明: {champion_en}")
            return result

        url = f"{DDRAGON_BASE}/cdn/{self._version}/img/champion/{champ_id}.png"
        dest = self.icons_dir / f"{champion_en}_0.png"

        # セッション上限チェック
        if self._session_downloads >= _MAX_DOWNLOADS_PER_SESSION:
            result.errors.append(
                f"セッションDL上限到達 ({_MAX_DOWNLOADS_PER_SESSION})")
            return result

        try:
            downloaded = _download_file(url, dest, overwrite=overwrite)
            if downloaded:
                result.downloaded.append(str(dest))
                self._session_downloads += 1
            else:
                result.skipped.append(str(dest))
        except Exception as e:
            self._session_downloads += 1  # 失敗もカウント
            result.failed.append(str(dest))
            result.errors.append(f"DL失敗 ({dest.name}): {e}")

        return result

    def download_all_for_champions(
        self,
        champion_names: list[str] | None = None,
        max_skins: int = 3,
        overwrite: bool = False,
    ) -> DownloadResult:
        """指定チャンピオン（またはマッピング全体）の素材を一括DL

        Args:
            champion_names: 日本語名リスト。Noneの場合はCHAMPION_NAME_MAP全件
            max_skins: スプラッシュのスキン数上限
            overwrite: 既存ファイルを上書きするか
        """
        total = DownloadResult()
        self._ensure_data()

        if champion_names is None:
            targets = list(CHAMPION_NAME_MAP.values())
        else:
            targets = []
            for jp_name in champion_names:
                en = CHAMPION_NAME_MAP.get(jp_name)
                if en:
                    targets.append(en)
                else:
                    total.errors.append(f"マッピングなし: {jp_name}")

        print(f"\n=== Riot素材ダウンロード: {len(targets)}チャンピオン ===")

        for i, en_name in enumerate(targets, 1):
            # セッション上限チェック
            if self._session_downloads >= _MAX_DOWNLOADS_PER_SESSION:
                print(f"  [中止] セッションDL上限到達 ({_MAX_DOWNLOADS_PER_SESSION})")
                break

            print(f"  [{i}/{len(targets)}] {en_name}...")

            # スプラッシュアート
            splash_result = self.download_champion_splash(
                en_name, max_skins=max_skins, overwrite=overwrite
            )
            total.downloaded.extend(splash_result.downloaded)
            total.skipped.extend(splash_result.skipped)
            total.failed.extend(splash_result.failed)
            total.errors.extend(splash_result.errors)

            # アイコン
            icon_result = self.download_champion_icon(
                en_name, overwrite=overwrite
            )
            total.downloaded.extend(icon_result.downloaded)
            total.skipped.extend(icon_result.skipped)
            total.failed.extend(icon_result.failed)
            total.errors.extend(icon_result.errors)

            time.sleep(_REQUEST_DELAY)

        print(f"\n  完了: DL={len(total.downloaded)}, "
              f"スキップ={len(total.skipped)}, "
              f"失敗={len(total.failed)}")
        if total.errors:
            for err in total.errors:
                print(f"  エラー: {err}")

        return total

    def download_missing_only(
        self,
        champion_names: list[str],
        asset_finder,
    ) -> DownloadResult:
        """不足素材のみダウンロード

        asset_finderで検索して見つからないチャンピオンの素材のみDLする。

        Args:
            champion_names: 日本語名リスト
            asset_finder: AssetFinderインスタンス
        """
        missing_champions = []
        for jp_name in champion_names:
            en_name = CHAMPION_NAME_MAP.get(jp_name, "")
            if not en_name:
                continue
            # スプラッシュまたはアイコンが無ければDL対象
            splash = asset_finder.find_splash(jp_name)
            icon = asset_finder.find_icon(jp_name)
            if not splash or not icon:
                missing_champions.append(jp_name)

        if not missing_champions:
            print("  不足素材なし（全チャンピオンの素材が揃っています）")
            return DownloadResult()

        print(f"  不足: {', '.join(missing_champions)}")
        return self.download_all_for_champions(
            champion_names=missing_champions, max_skins=3
        )
