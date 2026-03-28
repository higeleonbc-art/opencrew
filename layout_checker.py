"""レイアウトQAモジュール

レンダリングしたフレーム画像をClaude Vision APIに送り、
字幕の改行位置・レイアウト・素材の適切さを確認する。
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field

import anthropic
from PIL import Image


@dataclass
class LayoutIssue:
    """発見されたレイアウト問題"""
    severity: str = "warning"    # "error", "warning", "info"
    category: str = ""           # "subtitle", "asset", "layout", "composition"
    description: str = ""
    suggestion: str = ""


@dataclass
class LayoutCheckResult:
    """レイアウトチェックの結果"""
    passed: bool = True
    issues: list[LayoutIssue] = field(default_factory=list)
    overall_score: float = 1.0   # 0.0〜1.0


class LayoutChecker:
    """Claude Vision APIによるレイアウト品質チェック"""

    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def check_frame(
        self,
        frame: Image.Image,
        context: str = "",
        line_text: str = "",
    ) -> LayoutCheckResult:
        """フレーム画像のレイアウトをチェック

        Args:
            frame: チェック対象のフレーム画像
            context: このフレームの場面説明
            line_text: 表示されている字幕テキスト

        Returns:
            LayoutCheckResult
        """
        # 画像をBase64に変換
        buf = io.BytesIO()
        frame_rgb = frame.convert("RGB")
        frame_rgb.save(buf, format="JPEG", quality=85)
        img_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

        prompt = f"""この動画フレーム（YouTube Shorts, 1080x1920 縦長）のレイアウトを確認してください。

場面: {context or "不明"}
字幕テキスト: {line_text or "不明"}

以下の観点でチェックして、JSON形式で結果を返してください:

1. 字幕テキストの改行位置は自然か（単語の途中で切れていないか）
2. テキストが画面外にはみ出していないか
3. 素材画像がタイトル帯や字幕帯に重なっていないか
4. 素材画像の配置バランスは適切か
5. 全体的な視認性（文字が読みやすいか）

JSONのみを返してください:
{{
  "passed": true/false,
  "overall_score": 0.0-1.0,
  "issues": [
    {{
      "severity": "error" or "warning" or "info",
      "category": "subtitle" or "asset" or "layout" or "composition",
      "description": "問題の説明",
      "suggestion": "修正提案"
    }}
  ]
}}"""

        try:
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
                                "media_type": "image/jpeg",
                                "data": img_data,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )

            text = response.content[0].text.strip()
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not json_match:
                return LayoutCheckResult()

            data = json.loads(json_match.group())

            issues = []
            for issue_data in data.get("issues", []):
                issues.append(LayoutIssue(
                    severity=issue_data.get("severity", "warning"),
                    category=issue_data.get("category", ""),
                    description=issue_data.get("description", ""),
                    suggestion=issue_data.get("suggestion", ""),
                ))

            return LayoutCheckResult(
                passed=data.get("passed", True),
                issues=issues,
                overall_score=data.get("overall_score", 1.0),
            )

        except Exception as e:
            return LayoutCheckResult(
                passed=True,
                issues=[LayoutIssue(
                    severity="info",
                    category="system",
                    description=f"チェック実行エラー: {e}",
                )],
            )

    def check_key_frames(
        self,
        frames: list[tuple[Image.Image, str, str]],
        stop_on_error: bool = True,
    ) -> list[LayoutCheckResult]:
        """複数のキーフレームを一括チェック

        Args:
            frames: [(frame_image, context, line_text), ...]
            stop_on_error: エラー発見時に即停止するか

        Returns:
            各フレームのチェック結果リスト
        """
        results = []
        for frame, context, line_text in frames:
            result = self.check_frame(frame, context, line_text)
            results.append(result)
            if stop_on_error and not result.passed:
                break
        return results
