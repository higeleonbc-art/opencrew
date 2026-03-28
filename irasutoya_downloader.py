"""いらすとや素材ダウンロードモジュール

いらすとや (https://www.irasutoya.com) から動画に必要な素材を
検索・ダウンロードする。

【利用規約準拠ルール】
- 商用利用: 1動画あたりサムネイル含め20点まで無料
- 21点以上は有料（1点1,100円、全点数が課金対象）
- 同一イラストの重複使用は1点とカウント
- クレジット表記: 不要（ただし著作権は放棄されていない）
- 加工・合成: OK（品位を損なう加工は禁止）
- 素材自体の再配布・販売は禁止
- 公序良俗に反する利用は禁止

このモジュールは20点制限をカウント・管理し、
規約違反を防止する仕組みを組み込んでいる。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# いらすとやのベースURL
IRASUTOYA_BASE = "https://www.irasutoya.com"
IRASUTOYA_SEARCH = "https://www.irasutoya.com/search"

# 1動画あたりの無料利用上限（サムネイル含む）
MAX_FREE_ILLUSTRATIONS = 20

# リクエスト間のスリープ（サーバー負荷軽減）
_REQUEST_DELAY = 2.0

# User-Agent（礼儀として明示）
_HEADERS = {
    "User-Agent": "OpenCrew-VideoTool/0.1 (educational/personal project)",
}


@dataclass
class IrasutoyaItem:
    """いらすとや素材の情報"""
    title: str = ""
    page_url: str = ""
    image_url: str = ""
    local_path: str = ""
    keyword: str = ""
    content_hash: str = ""  # 重複判定用

    @property
    def is_downloaded(self) -> bool:
        return bool(self.local_path) and Path(self.local_path).exists()


@dataclass
class UsageTracker:
    """利用規約準拠のための使用点数トラッカー

    同一イラスト（content_hash）の重複使用は1点とカウント。
    1動画あたり20点を超えると警告を出す。
    """
    project_id: str = ""
    used_hashes: set[str] = field(default_factory=set)
    usage_log: list[dict] = field(default_factory=list)

    @property
    def unique_count(self) -> int:
        """ユニークなイラスト使用点数"""
        return len(self.used_hashes)

    @property
    def remaining(self) -> int:
        """残り無料利用可能点数"""
        return max(0, MAX_FREE_ILLUSTRATIONS - self.unique_count)

    @property
    def is_over_limit(self) -> bool:
        return self.unique_count > MAX_FREE_ILLUSTRATIONS

    def register_use(self, item: IrasutoyaItem, context: str = "") -> bool:
        """素材の使用を登録

        Returns:
            True=新規カウント、False=重複（カウント増えず）
        """
        is_new = item.content_hash not in self.used_hashes
        self.used_hashes.add(item.content_hash)
        self.usage_log.append({
            "hash": item.content_hash,
            "title": item.title,
            "context": context,
            "is_new_count": is_new,
            "total_after": self.unique_count,
        })
        return is_new

    def check_can_use(self) -> tuple[bool, str]:
        """追加使用が規約内か判定

        Returns:
            (使用可能か, メッセージ)
        """
        if self.unique_count >= MAX_FREE_ILLUSTRATIONS:
            return False, (
                f"いらすとや素材が{MAX_FREE_ILLUSTRATIONS}点に達しています。"
                f"商用利用の場合、これ以上の使用は有償（1点1,100円）となります。"
                f"現在 {self.unique_count}点使用中。"
            )
        if self.unique_count >= MAX_FREE_ILLUSTRATIONS - 3:
            return True, (
                f"注意: いらすとや素材が残り{self.remaining}点です "
                f"（{self.unique_count}/{MAX_FREE_ILLUSTRATIONS}点使用中）"
            )
        return True, f"OK ({self.unique_count}/{MAX_FREE_ILLUSTRATIONS}点使用中)"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "unique_count": self.unique_count,
            "max_free": MAX_FREE_ILLUSTRATIONS,
            "used_hashes": sorted(self.used_hashes),
            "usage_log": self.usage_log,
        }

    def save(self, path: Path) -> None:
        """トラッカー状態を保存"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: Path) -> UsageTracker:
        """トラッカー状態を読み込み"""
        if not path.exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        tracker = cls(
            project_id=data.get("project_id", ""),
            used_hashes=set(data.get("used_hashes", [])),
            usage_log=data.get("usage_log", []),
        )
        return tracker


def _compute_hash(data: bytes) -> str:
    """画像データのハッシュを計算（重複判定用）"""
    return hashlib.sha256(data).hexdigest()[:16]


def search_irasutoya(keyword: str, max_results: int = 10) -> list[IrasutoyaItem]:
    """いらすとやサイトをキーワード検索

    Google Bloggerベースの検索機能を利用。

    Args:
        keyword: 検索キーワード（日本語）
        max_results: 最大取得件数

    Returns:
        マッチした素材のリスト
    """
    items: list[IrasutoyaItem] = []

    params = {"q": keyword, "max-results": str(max_results)}
    try:
        resp = requests.get(
            IRASUTOYA_SEARCH, params=params, headers=_HEADERS, timeout=15
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  検索失敗 ({keyword}): {e}")
        return items

    soup = BeautifulSoup(resp.text, "html.parser")

    # いらすとやはBlogger形式。記事一覧から画像URLを抽出
    for post in soup.select(".post"):
        title_elem = post.select_one(".post-title a, h2 a, h3 a")
        if not title_elem:
            continue

        title = title_elem.get_text(strip=True)
        page_url = title_elem.get("href", "")

        # 記事内の画像を取得
        img_elem = post.select_one(".post-body img, .separator img, .entry img")
        if not img_elem:
            continue

        image_url = img_elem.get("src", "")
        if not image_url:
            continue

        # いらすとやの画像URLを正規化（高解像度版を取得）
        # Bloggerの画像URLは s72-c や s200 などのサイズ指定がある
        image_url = re.sub(r"/s\d+(-c)?/", "/s800/", image_url)

        items.append(IrasutoyaItem(
            title=title,
            page_url=page_url,
            image_url=image_url,
            keyword=keyword,
        ))

        if len(items) >= max_results:
            break

    return items


class IrasutoyaDownloader:
    """いらすとや素材ダウンローダー（利用規約準拠）"""

    def __init__(
        self,
        save_dir: str | Path,
        tracker_path: str | Path | None = None,
    ):
        """
        Args:
            save_dir: 素材保存先ディレクトリ
            tracker_path: 使用点数トラッカーの保存パス
        """
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)

        self._tracker_path = (
            Path(tracker_path) if tracker_path
            else self.save_dir / ".irasutoya_usage.json"
        )
        self.tracker = UsageTracker.load(self._tracker_path)

    def search(self, keyword: str, max_results: int = 5) -> list[IrasutoyaItem]:
        """キーワードで素材を検索"""
        print(f"  いらすとや検索: 「{keyword}」")
        items = search_irasutoya(keyword, max_results=max_results)
        print(f"  {len(items)}件見つかりました")
        return items

    def download(
        self,
        item: IrasutoyaItem,
        filename: str | None = None,
    ) -> IrasutoyaItem:
        """素材をダウンロード

        Args:
            item: ダウンロード対象
            filename: 保存ファイル名（None=自動生成）

        Returns:
            local_pathが設定されたIrasutoyaItem
        """
        # 使用点数チェック
        can_use, msg = self.tracker.check_can_use()
        if not can_use:
            print(f"  [警告] {msg}")
            print("  ダウンロードを中止します。")
            return item

        if not item.image_url:
            print(f"  画像URLなし: {item.title}")
            return item

        try:
            resp = requests.get(
                item.image_url, headers=_HEADERS, timeout=30
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as e:
            print(f"  DL失敗 ({item.title}): {e}")
            return item

        # ハッシュ計算（重複判定）
        item.content_hash = _compute_hash(data)

        # ファイル名決定
        if filename is None:
            # キーワード + ハッシュで一意なファイル名
            safe_keyword = re.sub(r'[^\w]', '_', item.keyword or "irasutoya")
            filename = f"{safe_keyword}_{item.content_hash}.png"

        dest = self.save_dir / filename

        # 既にダウンロード済みか確認
        if dest.exists():
            item.local_path = str(dest)
            # 重複でもトラッカーに登録（カウントは増えない）
            self.tracker.register_use(item, context="re-download")
            self._save_tracker()
            print(f"  既存: {dest.name}")
            return item

        # 保存
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)

        item.local_path = str(dest)

        # 使用登録
        is_new = self.tracker.register_use(item, context="download")
        self._save_tracker()

        status = "新規" if is_new else "重複（カウント増なし）"
        print(f"  保存: {dest.name} [{status}] "
              f"({self.tracker.unique_count}/{MAX_FREE_ILLUSTRATIONS}点)")

        time.sleep(_REQUEST_DELAY)
        return item

    def search_and_download(
        self,
        keyword: str,
        max_download: int = 1,
    ) -> list[IrasutoyaItem]:
        """検索してトップ結果をダウンロード

        Args:
            keyword: 検索キーワード
            max_download: ダウンロードする最大数

        Returns:
            ダウンロード済みアイテムのリスト
        """
        # 点数チェック
        can_use, msg = self.tracker.check_can_use()
        print(f"  使用状況: {msg}")
        if not can_use:
            return []

        items = self.search(keyword, max_results=max_download + 5)
        downloaded = []

        for item in items[:max_download]:
            result = self.download(item)
            if result.is_downloaded:
                downloaded.append(result)

        return downloaded

    def download_for_contexts(
        self,
        context_keywords: dict[str, str],
    ) -> dict[str, list[IrasutoyaItem]]:
        """場面コンテキストごとに必要な素材をDL

        Args:
            context_keywords: {"battle": "戦い", "sadness": "泣く", ...}

        Returns:
            {コンテキスト: [ダウンロードされたアイテム]}
        """
        results: dict[str, list[IrasutoyaItem]] = {}

        print(f"\n=== いらすとや素材ダウンロード ===")
        print(f"  現在の使用点数: {self.tracker.unique_count}/{MAX_FREE_ILLUSTRATIONS}")

        for context, keyword in context_keywords.items():
            # ローカルに既にキーワードのファイルがあればスキップ
            existing = list(self.save_dir.glob(f"*{keyword}*"))
            if existing:
                print(f"  [{context}] 「{keyword}」→ ローカルに{len(existing)}件あり、スキップ")
                results[context] = []
                continue

            can_use, msg = self.tracker.check_can_use()
            if not can_use:
                print(f"  [中止] {msg}")
                break

            print(f"  [{context}] 「{keyword}」を検索...")
            downloaded = self.search_and_download(keyword, max_download=1)
            results[context] = downloaded

        print(f"\n  最終使用点数: {self.tracker.unique_count}/{MAX_FREE_ILLUSTRATIONS}")
        return results

    def get_usage_report(self) -> str:
        """利用状況レポートを返す"""
        lines = [
            "=== いらすとや利用状況レポート ===",
            f"ユニーク素材数: {self.tracker.unique_count}/{MAX_FREE_ILLUSTRATIONS}点",
            f"残り無料枠: {self.tracker.remaining}点",
        ]
        if self.tracker.is_over_limit:
            over = self.tracker.unique_count - MAX_FREE_ILLUSTRATIONS
            lines.append(
                f"[警告] {over}点超過 → 有償利用が必要です（全{self.tracker.unique_count}点が課金対象）"
            )
        lines.append("")
        for log_entry in self.tracker.usage_log:
            mark = "*" if log_entry["is_new_count"] else " "
            lines.append(
                f"  {mark} {log_entry['title'][:40]} "
                f"(#{log_entry['total_after']})"
            )
        return "\n".join(lines)

    def _save_tracker(self) -> None:
        self.tracker.save(self._tracker_path)
