"""デスクトップ通知 + ターミナル確認プロンプト

素材不足の通知や、AI判断の確認を人間に求める。
plyer が利用可能ならデスクトップ通知、なければprint()にフォールバック。
"""

from __future__ import annotations

import sys

try:
    from plyer import notification as _plyer_notification
    _HAS_PLYER = True
except ImportError:
    _HAS_PLYER = False


class Notifier:
    """デスクトップ通知 + ターミナル確認"""

    def __init__(self, enabled: bool = True, app_name: str = "OpenCrew"):
        self.enabled = enabled
        self.app_name = app_name

    def notify(self, title: str, message: str) -> None:
        """デスクトップ通知を送信"""
        if not self.enabled:
            return

        print(f"\n[{self.app_name}] {title}")
        print(f"  {message}")

        if _HAS_PLYER:
            try:
                _plyer_notification.notify(
                    title=f"[{self.app_name}] {title}",
                    message=message,
                    app_name=self.app_name,
                    timeout=10,
                )
            except Exception:
                pass  # デスクトップ通知が失敗してもターミナル出力はされている

    def notify_missing_assets(self, missing: list[str]) -> None:
        """不足素材の通知"""
        if not missing:
            return
        msg = "以下の素材が不足しています:\n" + "\n".join(f"  - {m}" for m in missing)
        self.notify("素材不足", msg)

    def prompt_confirm(
        self,
        message: str,
        choices: list[str] | None = None,
        default: str = "y",
    ) -> str:
        """ターミナルで確認プロンプトを表示

        Args:
            message: 確認メッセージ
            choices: 選択肢リスト（Noneの場合はy/n）
            default: デフォルトの選択

        Returns:
            ユーザーの入力（小文字）
        """
        if choices is None:
            choices = ["y", "n"]

        choices_str = "/".join(
            c.upper() if c == default else c for c in choices
        )
        prompt = f"\n[{self.app_name}] {message} [{choices_str}]: "

        try:
            user_input = input(prompt).strip().lower()
            if not user_input:
                return default
            if user_input in choices:
                return user_input
            return default
        except (EOFError, KeyboardInterrupt):
            print()
            return default

    def prompt_select(
        self,
        message: str,
        options: list[str],
        allow_skip: bool = True,
    ) -> int | None:
        """番号付き選択肢から選ばせるプロンプト

        Args:
            message: メッセージ
            options: 選択肢リスト
            allow_skip: スキップ可能か

        Returns:
            選択されたインデックス。スキップ時はNone
        """
        print(f"\n[{self.app_name}] {message}")
        for i, opt in enumerate(options):
            print(f"  {i + 1}. {opt}")
        if allow_skip:
            print(f"  0. スキップ")

        try:
            prompt = "番号を入力: "
            user_input = input(prompt).strip()
            if not user_input:
                return 0 if not allow_skip else None
            num = int(user_input)
            if allow_skip and num == 0:
                return None
            if 1 <= num <= len(options):
                return num - 1
            return 0 if not allow_skip else None
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
            return None

    def show_preview_and_confirm(
        self,
        description: str,
        preview_path: str | None = None,
    ) -> bool:
        """プレビュー画像のパスを表示して確認を求める

        Args:
            description: 何のプレビューか
            preview_path: プレビュー画像のパス（あれば表示指示）

        Returns:
            承認されたかどうか
        """
        print(f"\n[{self.app_name}] プレビュー: {description}")
        if preview_path:
            print(f"  画像: {preview_path}")
            # デスクトップ通知でも知らせる
            self.notify("確認待ち", f"{description}\n{preview_path}")

        result = self.prompt_confirm("この結果を承認しますか？")
        return result == "y"
