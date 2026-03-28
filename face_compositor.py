"""顔合成モジュール

Claude Vision APIを使っていらすとや画像の顔位置を検出し、
チャンピオンアイコンで顔を差し替える。

フロー:
1. いらすとや画像をClaude Visionに送信 → 顔のバウンディングボックスを取得
2. バウンディングボックスにチャンピオンアイコンをリサイズして合成
3. 結果を人間に確認してもらう（初期段階）
4. 確認済みの顔位置はSQLiteにキャッシュ
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw

from .decision_store import DecisionStore, FacePosition


@dataclass
class FaceBBox:
    """顔のバウンディングボックス"""
    x: int          # 左上X
    y: int          # 左上Y
    width: int
    height: int
    label: str = "" # "person_1", "person_2" 等の識別ラベル

    def to_dict(self) -> dict:
        return {
            "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FaceBBox:
        return cls(
            x=d["x"], y=d["y"],
            width=d["width"], height=d["height"],
            label=d.get("label", ""),
        )


@dataclass
class CompositeRequest:
    """合成リクエスト"""
    irasutoya_path: str
    icon_paths: list[str]          # 顔に割り当てるアイコン（左から順）
    champion_names: list[str] = field(default_factory=list)
    faces: list[FaceBBox] = field(default_factory=list)


@dataclass
class CompositeResult:
    """合成結果"""
    image: Image.Image | None = None
    faces_detected: list[FaceBBox] = field(default_factory=list)
    success: bool = False
    error: str = ""


def _image_to_base64(img: Image.Image, format: str = "PNG") -> str:
    """PIL ImageをBase64文字列に変換"""
    buf = io.BytesIO()
    img.save(buf, format=format)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def _load_image_base64(path: str) -> tuple[str, str]:
    """画像ファイルをBase64 + media_typeで読み込み"""
    p = Path(path)
    suffix = p.suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_type_map.get(suffix, "image/png")

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


class FaceCompositor:
    """Claude Vision APIを使った顔検出＋合成エンジン"""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        store: DecisionStore | None = None,
    ):
        self.client = client or anthropic.Anthropic()
        self.store = store

    def detect_faces(self, irasutoya_path: str) -> list[FaceBBox]:
        """Claude Vision APIでいらすとや画像の顔位置を検出

        Args:
            irasutoya_path: いらすとや画像のパス

        Returns:
            検出された顔のバウンディングボックスリスト（左→右の順）
        """
        # キャッシュチェック
        if self.store:
            cached = self.store.get_face_positions(irasutoya_path)
            if cached and cached.confirmed:
                return [FaceBBox.from_dict(f) for f in cached.faces]

        # Vision APIで検出
        img_data, media_type = _load_image_base64(irasutoya_path)

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": """この画像に描かれているキャラクターの顔の位置を検出してください。

各キャラクターの顔について、バウンディングボックスの座標をJSON配列で返してください。
左にいるキャラクターから順に、以下の形式で：

[
  {"x": 左上X座標, "y": 左上Y座標, "width": 幅, "height": 高さ, "label": "person_1"},
  {"x": 左上X座標, "y": 左上Y座標, "width": 幅, "height": 高さ, "label": "person_2"}
]

座標はピクセル単位で、画像の左上が原点です。
顔だけでなく、頭部全体を含むバウンディングボックスにしてください。
JSONのみを返してください（説明文は不要）。""",
                    },
                ],
            }],
        )

        text = response.content[0].text.strip()
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if not json_match:
            return []

        faces_data = json.loads(json_match.group())
        faces = [FaceBBox.from_dict(f) for f in faces_data]

        # X座標でソート（左→右）
        faces.sort(key=lambda f: f.x)

        # キャッシュに保存（未確認状態）
        if self.store:
            fp = FacePosition(
                irasutoya_path=irasutoya_path,
                faces_json=json.dumps([f.to_dict() for f in faces]),
                confirmed=False,
            )
            self.store.save_face_positions(fp)

        return faces

    def composite(self, request: CompositeRequest) -> CompositeResult:
        """いらすとや画像にチャンピオンアイコンを合成

        Args:
            request: 合成リクエスト（いらすとやパス、アイコンパス、顔情報）

        Returns:
            CompositeResult: 合成結果
        """
        try:
            # いらすとや画像を読み込み
            base_img = Image.open(request.irasutoya_path).convert("RGBA")

            # 顔位置が未検出なら検出
            faces = request.faces
            if not faces:
                faces = self.detect_faces(request.irasutoya_path)

            if not faces:
                return CompositeResult(
                    error="顔を検出できませんでした",
                    faces_detected=[],
                )

            # アイコン数と顔数の調整
            icons = request.icon_paths[:len(faces)]
            if len(icons) < len(faces):
                # アイコンが足りない場合、最後のアイコンを繰り返す
                while len(icons) < len(faces) and icons:
                    icons.append(icons[-1])

            # 各顔にアイコンを合成
            result_img = base_img.copy()

            for face, icon_path in zip(faces, icons):
                icon_img = Image.open(icon_path).convert("RGBA")

                # アイコンを顔サイズにリサイズ
                # 少し大きめにして顔全体をカバー
                target_size = max(face.width, face.height)
                padding = int(target_size * 0.15)  # 15%のパディング
                target_size += padding

                icon_resized = icon_img.resize(
                    (target_size, target_size),
                    Image.LANCZOS,
                )

                # 丸形にマスク
                mask = Image.new("L", (target_size, target_size), 0)
                draw = ImageDraw.Draw(mask)
                draw.ellipse([0, 0, target_size - 1, target_size - 1], fill=255)

                # 枠線用のマスク
                border_width = max(3, target_size // 25)
                border_mask = Image.new("L", (target_size, target_size), 0)
                border_draw = ImageDraw.Draw(border_mask)
                border_draw.ellipse(
                    [0, 0, target_size - 1, target_size - 1], fill=255
                )
                border_draw.ellipse(
                    [border_width, border_width,
                     target_size - 1 - border_width,
                     target_size - 1 - border_width],
                    fill=0,
                )

                # 枠線レイヤー（黒）
                border_layer = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 255))

                # アイコンを丸く切り抜き
                icon_masked = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
                icon_masked.paste(icon_resized, (0, 0), mask)

                # 合成位置を計算（顔の中心にアイコンの中心を合わせる）
                center_x = face.x + face.width // 2
                center_y = face.y + face.height // 2
                paste_x = center_x - target_size // 2
                paste_y = center_y - target_size // 2

                # 枠線 → アイコンの順で合成
                result_img.paste(border_layer, (paste_x, paste_y), border_mask)
                result_img.paste(icon_masked, (paste_x, paste_y), icon_masked)

            return CompositeResult(
                image=result_img,
                faces_detected=faces,
                success=True,
            )

        except Exception as e:
            return CompositeResult(error=str(e))

    def generate_preview(
        self, request: CompositeRequest, max_size: int = 800
    ) -> Image.Image | None:
        """確認用プレビュー画像を生成

        合成結果 + 検出した顔位置のバウンディングボックスを表示
        """
        result = self.composite(request)
        if not result.success or result.image is None:
            return None

        preview = result.image.copy()
        draw = ImageDraw.Draw(preview)

        # バウンディングボックスを赤枠で表示
        for i, face in enumerate(result.faces_detected):
            draw.rectangle(
                [face.x, face.y, face.x + face.width, face.y + face.height],
                outline=(255, 0, 0, 200),
                width=2,
            )
            draw.text(
                (face.x, face.y - 15),
                f"Face {i+1}",
                fill=(255, 0, 0, 255),
            )

        # リサイズ
        w, h = preview.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            preview = preview.resize(
                (int(w * ratio), int(h * ratio)),
                Image.LANCZOS,
            )

        return preview
