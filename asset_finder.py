"""素材検索モジュール

設定されたディレクトリから、チャンピオン名・キーワードに基づいて
素材ファイルを検索する。

命名規則:
  splash/  → {チャンピオン名}_{番号}.png  (0=デフォルト背景, 1+=別スキン)
  icons/   → {チャンピオン名}_{番号}.png  (0=デフォルト, 1+=別スキン)
  irasutoya/ → {キーワード}.png
  cinematic/ → 任意のファイル名（チャンピオン名は含まない。手動配置）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .script_analyzer import CHAMPION_NAME_MAP


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}

# ファイル名から番号を抽出するパターン: チャンピオン名_0.png → 0
_SKIN_NUMBER_RE = re.compile(r"_(\d+)\.\w+$")


@dataclass
class AssetMatch:
    """検索でマッチした素材"""
    path: str
    asset_type: str        # "splash", "cinematic", "icon", "irasutoya"
    champion_name: str = ""
    keyword: str = ""
    skin_number: int = 0   # 0=デフォルト, 1+=別スキン
    score: float = 1.0     # マッチスコア（完全一致=1.0, 部分一致=0.5等）


@dataclass
class AssetSearchResult:
    """素材検索の結果"""
    found: list[AssetMatch] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _extract_skin_number(path: Path) -> int:
    """ファイル名から番号を抽出（シェン_0.png → 0, シェン_2.png → 2）"""
    m = _SKIN_NUMBER_RE.search(path.name)
    return int(m.group(1)) if m else 0


class AssetFinder:
    """素材ディレクトリを走査して適切な素材を検索"""

    def __init__(self, asset_dirs: dict[str, str]):
        """
        Args:
            asset_dirs: {"splash": "/path/to/splash", "cinematic": "/path/to/cinematic", ...}
        """
        self.asset_dirs = {k: Path(v) for k, v in asset_dirs.items()}

    def _glob_files(
        self, base_dir: Path, pattern: str, exts: set[str] | None = None
    ) -> list[Path]:
        """ディレクトリをglobして拡張子フィルタ"""
        if not base_dir.exists():
            return []
        results = []
        for p in base_dir.glob(pattern):
            if p.is_file():
                if exts is None or p.suffix.lower() in exts:
                    results.append(p)
        return sorted(results)

    def _find_numbered_assets(
        self, base_dir: Path, champion_name: str, asset_type: str, exts: set[str],
    ) -> list[AssetMatch]:
        """番号付き命名規則でチャンピオン素材を検索

        {チャンピオン名}_{番号}.ext のパターンで検索し、
        番号順にソートして返す。
        """
        if not base_dir or not base_dir.exists():
            return []

        matches: list[AssetMatch] = []
        seen: set[str] = set()

        # 日本語名・英語名の両方で検索
        names = [champion_name]
        en_name = CHAMPION_NAME_MAP.get(champion_name, "")
        if en_name:
            names.append(en_name)

        for name in names:
            # {名前}_*.ext パターン
            for p in self._glob_files(base_dir, f"{name}_*", exts):
                if str(p) not in seen:
                    seen.add(str(p))
                    skin_num = _extract_skin_number(p)
                    matches.append(AssetMatch(
                        path=str(p), asset_type=asset_type,
                        champion_name=champion_name,
                        skin_number=skin_num,
                        score=1.0,
                    ))
            # サブディレクトリも検索
            for p in self._glob_files(base_dir, f"**/{name}_*", exts):
                if str(p) not in seen:
                    seen.add(str(p))
                    skin_num = _extract_skin_number(p)
                    matches.append(AssetMatch(
                        path=str(p), asset_type=asset_type,
                        champion_name=champion_name,
                        skin_number=skin_num,
                        score=0.8,
                    ))

        # 番号順にソート（0=デフォルトが先頭）
        matches.sort(key=lambda m: m.skin_number)
        return matches

    def find_splash(self, champion_name: str) -> list[AssetMatch]:
        """チャンピオンのスプラッシュアートを検索

        命名規則: {チャンピオン名}_{番号}.png
        - _0 = デフォルト（背景に使用）
        - _1以上 = 別スキン（コンテンツエリアに配置）
        """
        base = self.asset_dirs.get("splash")
        return self._find_numbered_assets(base, champion_name, "splash", IMAGE_EXTS)

    def find_splash_default(self, champion_name: str) -> AssetMatch | None:
        """デフォルトスプラッシュアート（_0）を取得。背景用。"""
        matches = self.find_splash(champion_name)
        for m in matches:
            if m.skin_number == 0:
                return m
        return matches[0] if matches else None

    def find_splash_skins(self, champion_name: str) -> list[AssetMatch]:
        """別スキンのスプラッシュアート（_1以上）を取得。コンテンツエリア用。"""
        return [m for m in self.find_splash(champion_name) if m.skin_number > 0]

    def find_cinematic(self) -> list[AssetMatch]:
        """シネマティック動画を全取得

        ※チャンピオン名はファイル名に含まれない。
        手動で配置された動画を全て返し、AIが内容から使える箇所を判断する。
        """
        base = self.asset_dirs.get("cinematic")
        if not base or not base.exists():
            return []

        matches = []
        for p in self._glob_files(base, "**/*", VIDEO_EXTS):
            matches.append(AssetMatch(
                path=str(p), asset_type="cinematic",
                score=1.0,
            ))
        return matches

    def find_icon(self, champion_name: str) -> list[AssetMatch]:
        """チャンピオンのアイコン画像を検索（顔合成用）

        命名規則: {チャンピオン名}_{番号}.png
        - _0 = デフォルトアイコン
        - _1以上 = 別スキンアイコン
        """
        base = self.asset_dirs.get("icons")
        return self._find_numbered_assets(base, champion_name, "icon", IMAGE_EXTS)

    def find_icon_default(self, champion_name: str) -> AssetMatch | None:
        """デフォルトアイコン（_0）を取得"""
        matches = self.find_icon(champion_name)
        for m in matches:
            if m.skin_number == 0:
                return m
        return matches[0] if matches else None

    def find_irasutoya(self, keyword: str) -> list[AssetMatch]:
        """いらすとや素材をキーワードで検索"""
        base = self.asset_dirs.get("irasutoya")
        if not base:
            return []

        matches = []
        for p in self._glob_files(base, f"**/*{keyword}*", IMAGE_EXTS):
            matches.append(AssetMatch(
                path=str(p), asset_type="irasutoya",
                keyword=keyword, score=1.0,
            ))

        # キーワードでヒットしない場合、全ファイルを候補として返す
        if not matches:
            for p in self._glob_files(base, "**/*", IMAGE_EXTS):
                matches.append(AssetMatch(
                    path=str(p), asset_type="irasutoya",
                    keyword=keyword, score=0.3,
                ))

        return matches

    def find_all_for_champion(self, champion_name: str) -> dict[str, list[AssetMatch]]:
        """チャンピオンの全種類の素材を一括検索"""
        return {
            "splash": self.find_splash(champion_name),
            "cinematic": self.find_cinematic(),
            "icon": self.find_icon(champion_name),
        }

    def check_missing(
        self, champions: list[str], need_cinematic: bool = False
    ) -> list[str]:
        """不足している素材のリストを返す"""
        missing = []
        for champ in champions:
            if not self.find_splash(champ):
                missing.append(f"スプラッシュアート: {champ}_0.png（デフォルト必須）")
            if not self.find_icon(champ):
                missing.append(f"アイコン画像: {champ}_0.png（いらすとや合成用）")
        if need_cinematic and not self.find_cinematic():
            missing.append("シネマティック動画: cinematic/フォルダに動画を配置してください")
        return missing

    def list_available_irasutoya(self) -> list[str]:
        """利用可能ないらすとや素材の一覧"""
        base = self.asset_dirs.get("irasutoya")
        if not base or not base.exists():
            return []
        return [
            str(p) for p in sorted(base.rglob("*"))
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        ]
