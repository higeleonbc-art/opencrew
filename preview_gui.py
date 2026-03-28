"""素材プレビューGUIモジュール

Tkinterを使用して素材のプレビュー・選択・確認をGUIで行う。
ターミナルのみだったプレビュー表示をGUIに拡張する。

機能:
- 素材画像のサムネイルプレビュー
- いらすとや合成結果のプレビュー
- 素材選択の承認/却下ボタン
- いらすとや使用点数の表示
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

try:
    import tkinter as tk
    from tkinter import ttk
    _HAS_TK = True
except ImportError:
    _HAS_TK = False


@dataclass
class PreviewRequest:
    """プレビュー表示リクエスト"""
    title: str = ""
    description: str = ""
    image_path: str = ""
    image: Image.Image | None = None  # PIL Imageを直接渡す場合
    line_index: int = -1
    asset_type: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class PreviewResponse:
    """プレビュー表示に対するユーザー応答"""
    approved: bool = False
    selected_index: int = -1
    comment: str = ""


def _has_display() -> bool:
    """ディスプレイ環境があるか判定"""
    if not _HAS_TK:
        return False
    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


def _pil_to_tk(image: Image.Image):
    """PIL ImageをTkinter PhotoImageに変換"""
    from PIL import ImageTk
    return ImageTk.PhotoImage(image)


def _load_preview_image(
    path: str | None = None,
    image: Image.Image | None = None,
    max_size: int = 600,
) -> Image.Image | None:
    """プレビュー用に画像をロード＆リサイズ"""
    if image is not None:
        img = image.copy()
    elif path and Path(path).exists():
        img = Image.open(path).convert("RGBA")
    else:
        return None

    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    return img


class PreviewWindow:
    """素材プレビューウィンドウ（Tkinter）"""

    def __init__(self):
        if not _HAS_TK:
            raise RuntimeError("tkinterが利用できません")

        self._result: PreviewResponse | None = None
        self._root: tk.Tk | None = None
        self._photo_refs: list = []  # PhotoImageの参照保持用

    def show_approval(self, request: PreviewRequest) -> PreviewResponse:
        """素材プレビューを表示して承認/却下を待つ

        メインスレッドで呼び出す必要がある。

        Args:
            request: プレビューリクエスト

        Returns:
            ユーザーの応答
        """
        self._result = PreviewResponse()
        self._photo_refs.clear()

        root = tk.Tk()
        self._root = root
        root.title(f"OpenCrew - {request.title}")
        root.configure(bg="#2b2b2b")

        # ウィンドウサイズ
        root.geometry("700x550")
        root.resizable(True, True)

        # スタイル設定
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background="#2b2b2b")
        style.configure("Dark.TLabel", background="#2b2b2b", foreground="#ffffff",
                        font=("sans-serif", 11))
        style.configure("Title.TLabel", background="#2b2b2b", foreground="#ffffff",
                        font=("sans-serif", 14, "bold"))
        style.configure("Approve.TButton", font=("sans-serif", 12))
        style.configure("Reject.TButton", font=("sans-serif", 12))

        main_frame = ttk.Frame(root, style="Dark.TFrame", padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # タイトル
        ttk.Label(
            main_frame, text=request.title, style="Title.TLabel"
        ).pack(pady=(0, 5))

        # 説明
        if request.description:
            ttk.Label(
                main_frame, text=request.description, style="Dark.TLabel",
                wraplength=650,
            ).pack(pady=(0, 10))

        # メタデータ表示
        if request.metadata:
            meta_frame = ttk.Frame(main_frame, style="Dark.TFrame")
            meta_frame.pack(fill=tk.X, pady=(0, 10))
            for key, value in request.metadata.items():
                ttk.Label(
                    meta_frame,
                    text=f"{key}: {value}",
                    style="Dark.TLabel",
                ).pack(anchor=tk.W)

        # 画像プレビュー
        img = _load_preview_image(
            path=request.image_path,
            image=request.image,
            max_size=500,
        )
        if img:
            # RGBA → RGB（Tkinterの PhotoImage は透過に対応していない場合がある）
            display_img = Image.new("RGB", img.size, (43, 43, 43))
            display_img.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)

            photo = _pil_to_tk(display_img)
            self._photo_refs.append(photo)

            img_label = tk.Label(main_frame, image=photo, bg="#1e1e1e",
                                borderwidth=2, relief=tk.SUNKEN)
            img_label.pack(pady=10)

        # ボタンフレーム
        btn_frame = ttk.Frame(main_frame, style="Dark.TFrame")
        btn_frame.pack(pady=10)

        def on_approve():
            self._result = PreviewResponse(approved=True)
            root.destroy()

        def on_reject():
            self._result = PreviewResponse(approved=False)
            root.destroy()

        approve_btn = ttk.Button(
            btn_frame, text="承認 (Y)", command=on_approve, style="Approve.TButton"
        )
        approve_btn.pack(side=tk.LEFT, padx=20)

        reject_btn = ttk.Button(
            btn_frame, text="却下 (N)", command=on_reject, style="Reject.TButton"
        )
        reject_btn.pack(side=tk.LEFT, padx=20)

        # キーボードショートカット
        root.bind("y", lambda e: on_approve())
        root.bind("Y", lambda e: on_approve())
        root.bind("n", lambda e: on_reject())
        root.bind("N", lambda e: on_reject())
        root.bind("<Return>", lambda e: on_approve())
        root.bind("<Escape>", lambda e: on_reject())

        # ウィンドウを前面に
        root.lift()
        root.attributes("-topmost", True)
        root.after(100, lambda: root.attributes("-topmost", False))
        root.focus_force()

        root.mainloop()
        self._root = None

        return self._result or PreviewResponse()

    def show_selection(
        self,
        title: str,
        options: list[PreviewRequest],
    ) -> int | None:
        """複数の素材から1つ選択するGUI

        Args:
            title: ウィンドウタイトル
            options: 選択肢リスト

        Returns:
            選択されたインデックス。キャンセル時はNone
        """
        self._result = PreviewResponse(selected_index=-1)
        self._photo_refs.clear()

        root = tk.Tk()
        self._root = root
        root.title(f"OpenCrew - {title}")
        root.configure(bg="#2b2b2b")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background="#2b2b2b")
        style.configure("Dark.TLabel", background="#2b2b2b", foreground="#ffffff",
                        font=("sans-serif", 10))
        style.configure("Title.TLabel", background="#2b2b2b", foreground="#ffffff",
                        font=("sans-serif", 13, "bold"))

        main_frame = ttk.Frame(root, style="Dark.TFrame", padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            main_frame, text=title, style="Title.TLabel"
        ).pack(pady=(0, 10))

        # スクロール可能なフレーム
        canvas = tk.Canvas(main_frame, bg="#2b2b2b", highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_frame, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas, style="Dark.TFrame")

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def make_select_handler(idx):
            def handler():
                self._result = PreviewResponse(selected_index=idx, approved=True)
                root.destroy()
            return handler

        # 各選択肢をグリッド表示
        cols = 3
        for i, opt in enumerate(options):
            row = i // cols
            col = i % cols

            item_frame = ttk.Frame(scroll_frame, style="Dark.TFrame", padding=5)
            item_frame.grid(row=row, column=col, padx=5, pady=5, sticky=tk.N)

            img = _load_preview_image(
                path=opt.image_path, image=opt.image, max_size=180
            )
            if img:
                display_img = Image.new("RGB", img.size, (43, 43, 43))
                display_img.paste(
                    img, mask=img.split()[3] if img.mode == "RGBA" else None
                )
                photo = _pil_to_tk(display_img)
                self._photo_refs.append(photo)

                btn = tk.Button(
                    item_frame, image=photo, command=make_select_handler(i),
                    bg="#1e1e1e", activebackground="#3e3e3e",
                    borderwidth=2, relief=tk.RAISED,
                )
                btn.pack()

            ttk.Label(
                item_frame,
                text=opt.title[:25] or f"素材 {i+1}",
                style="Dark.TLabel",
                wraplength=170,
            ).pack(pady=(3, 0))

        # キャンセルボタン
        def on_cancel():
            self._result = PreviewResponse(selected_index=-1)
            root.destroy()

        cancel_btn = ttk.Button(main_frame, text="キャンセル", command=on_cancel)
        cancel_btn.pack(pady=10)
        root.bind("<Escape>", lambda e: on_cancel())

        # ウィンドウサイズ調整
        total_items = len(options)
        rows = (total_items + cols - 1) // cols
        width = min(cols * 210 + 40, 700)
        height = min(rows * 260 + 100, 700)
        root.geometry(f"{width}x{height}")

        root.lift()
        root.attributes("-topmost", True)
        root.after(100, lambda: root.attributes("-topmost", False))
        root.focus_force()

        root.mainloop()
        self._root = None

        result = self._result or PreviewResponse()
        return result.selected_index if result.selected_index >= 0 else None


class PreviewManager:
    """プレビュー管理（GUI/ターミナル自動切り替え）

    GUI環境があればTkinterを使い、なければターミナルにフォールバック。
    """

    def __init__(self):
        self.gui_available = _has_display()
        self._window: PreviewWindow | None = None

        if self.gui_available:
            try:
                self._window = PreviewWindow()
            except Exception:
                self.gui_available = False

    def show_approval(
        self,
        title: str,
        description: str = "",
        image_path: str = "",
        image: Image.Image | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """素材プレビューを表示して承認/却下を取得

        Returns:
            承認されたか
        """
        request = PreviewRequest(
            title=title,
            description=description,
            image_path=image_path,
            image=image,
            metadata=metadata or {},
        )

        if self.gui_available and self._window:
            try:
                response = self._window.show_approval(request)
                return response.approved
            except Exception:
                pass  # GUIが失敗したらターミナルにフォールバック

        # ターミナルフォールバック
        return self._terminal_approval(request)

    def show_selection(
        self,
        title: str,
        options: list[dict],
    ) -> int | None:
        """複数の素材から選択

        Args:
            title: タイトル
            options: [{"title": ..., "image_path": ...}, ...]

        Returns:
            選択インデックス。キャンセル時はNone
        """
        requests = [
            PreviewRequest(
                title=opt.get("title", f"素材 {i+1}"),
                image_path=opt.get("image_path", ""),
            )
            for i, opt in enumerate(options)
        ]

        if self.gui_available and self._window:
            try:
                return self._window.show_selection(title, requests)
            except Exception:
                pass

        # ターミナルフォールバック
        return self._terminal_selection(title, options)

    def _terminal_approval(self, request: PreviewRequest) -> bool:
        """ターミナルでの承認確認"""
        print(f"\n[OpenCrew] プレビュー: {request.title}")
        if request.description:
            print(f"  {request.description}")
        if request.image_path:
            print(f"  画像: {request.image_path}")
        for k, v in request.metadata.items():
            print(f"  {k}: {v}")

        try:
            answer = input("\n  承認しますか？ [Y/n]: ").strip().lower()
            return answer != "n"
        except (EOFError, KeyboardInterrupt):
            print()
            return True

    def _terminal_selection(self, title: str, options: list[dict]) -> int | None:
        """ターミナルでの選択"""
        print(f"\n[OpenCrew] {title}")
        for i, opt in enumerate(options):
            name = opt.get("title", f"素材 {i+1}")
            path = opt.get("image_path", "")
            print(f"  {i+1}. {name}")
            if path:
                print(f"     {path}")
        print("  0. キャンセル")

        try:
            choice = input("\n  番号を選択: ").strip()
            if not choice:
                return 0
            num = int(choice)
            if num == 0:
                return None
            if 1 <= num <= len(options):
                return num - 1
            return 0
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
            return None
