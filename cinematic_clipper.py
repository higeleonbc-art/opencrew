"""シネマティック動画クリッピング自動化モジュール

シネマティック動画からロゴ・権利表記（クレジット）部分を
自動検出し、安全に使用できる区間を特定する。

検出手法:
1. ffprobeで動画メタデータ取得（長さ、解像度等）
2. 冒頭・末尾の一定秒数をロゴ・クレジット領域としてマーク
3. Claude Vision APIで各区間のフレームを解析し、
   ロゴ/クレジット/権利表記の有無を判定（オプション）
4. 安全な使用区間を返す

出力:
- ClipRange: 使用可能な時間範囲（start_sec, end_sec）
- ffmpegで切り出すためのコマンド生成
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False


@dataclass
class ClipRange:
    """使用可能な動画区間"""
    start_sec: float
    end_sec: float
    confidence: float = 1.0
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end_sec - self.start_sec

    def is_valid(self, min_duration: float = 1.0) -> bool:
        return self.duration >= min_duration


@dataclass
class VideoInfo:
    """動画メタデータ"""
    path: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""


@dataclass
class ClipResult:
    """クリッピング結果"""
    video_info: VideoInfo = field(default_factory=VideoInfo)
    safe_ranges: list[ClipRange] = field(default_factory=list)
    logo_ranges: list[ClipRange] = field(default_factory=list)  # ロゴ検出区間
    credit_ranges: list[ClipRange] = field(default_factory=list)  # クレジット区間
    success: bool = False
    error: str = ""


def get_video_info(video_path: str) -> VideoInfo:
    """ffprobeで動画メタデータを取得"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        video_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=30,
        )
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        data = json.loads(stdout_text)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, TypeError,
            FileNotFoundError, UnicodeDecodeError, AttributeError) as e:
        return VideoInfo(path=video_path)

    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0))

    video_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video_stream = stream
            break

    width = 0
    height = 0
    fps = 0.0
    codec = ""

    if video_stream:
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec = video_stream.get("codec_name", "")

        # FPS計算
        r_frame_rate = video_stream.get("r_frame_rate", "30/1")
        try:
            num, den = r_frame_rate.split("/")
            fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            fps = 30.0

    return VideoInfo(
        path=video_path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        codec=codec,
    )


def extract_frame(video_path: str, time_sec: float) -> Image.Image | None:
    """動画の指定時刻のフレームを抽出"""
    cmd = [
        "ffmpeg", "-v", "quiet",
        "-ss", str(time_sec),
        "-i", video_path,
        "-vframes", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=15
        )
        if result.returncode == 0 and result.stdout:
            return Image.open(io.BytesIO(result.stdout))
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def clip_video(
    video_path: str,
    output_path: str,
    start_sec: float,
    end_sec: float,
) -> bool:
    """ffmpegで動画を切り出し

    Args:
        video_path: 入力動画パス
        output_path: 出力動画パス
        start_sec: 開始秒
        end_sec: 終了秒

    Returns:
        成功したか
    """
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg", "-v", "quiet", "-y",
        "-ss", str(start_sec),
        "-i", video_path,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


class CinematicClipper:
    """シネマティック動画のクリッピング自動化"""

    # ルールベースのデフォルトマージン
    DEFAULT_INTRO_SKIP = 5.0    # 冒頭スキップ秒数（ロゴ回避）
    DEFAULT_OUTRO_SKIP = 8.0    # 末尾スキップ秒数（クレジット回避）
    MIN_CLIP_DURATION = 3.0     # 最小クリップ秒数

    def __init__(
        self,
        client: Optional[anthropic.Anthropic] = None,
        use_vision: bool = False,
        output_dir: str | Path = "./opencrew_assets/cinematic/clips",
    ):
        """
        Args:
            client: Claude APIクライアント（Vision解析用）
            use_vision: Vision APIでフレーム解析を行うか
            output_dir: クリップ出力先
        """
        self.client = client
        self.use_vision = use_vision and _HAS_ANTHROPIC and client is not None
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def analyze(self, video_path: str) -> ClipResult:
        """動画を解析して安全な使用区間を特定

        Args:
            video_path: シネマティック動画のパス

        Returns:
            ClipResult: 解析結果
        """
        result = ClipResult()

        # Step 1: メタデータ取得
        info = get_video_info(video_path)
        result.video_info = info

        if info.duration <= 0:
            result.error = "動画の長さを取得できません"
            return result

        print(f"  動画: {Path(video_path).name}")
        print(f"  長さ: {info.duration:.1f}秒, "
              f"{info.width}x{info.height}, {info.fps:.0f}fps")

        # Step 2: ルールベースの初期推定
        intro_skip = min(self.DEFAULT_INTRO_SKIP, info.duration * 0.15)
        outro_skip = min(self.DEFAULT_OUTRO_SKIP, info.duration * 0.2)

        # Step 3: Vision APIでの検証（オプション）
        if self.use_vision:
            intro_skip, outro_skip, logo_ranges, credit_ranges = (
                self._vision_analyze(video_path, info)
            )
            result.logo_ranges = logo_ranges
            result.credit_ranges = credit_ranges

        # Step 4: 安全区間を算出
        safe_start = intro_skip
        safe_end = info.duration - outro_skip

        if safe_end - safe_start >= self.MIN_CLIP_DURATION:
            result.safe_ranges.append(ClipRange(
                start_sec=safe_start,
                end_sec=safe_end,
                confidence=0.9 if self.use_vision else 0.7,
                reason="ロゴ・クレジット回避区間",
            ))
            result.success = True
        else:
            # 動画が短すぎる場合、マージンを縮小して再試行
            safe_start = min(2.0, info.duration * 0.1)
            safe_end = info.duration - min(3.0, info.duration * 0.1)
            if safe_end - safe_start >= self.MIN_CLIP_DURATION:
                result.safe_ranges.append(ClipRange(
                    start_sec=safe_start,
                    end_sec=safe_end,
                    confidence=0.5,
                    reason="短い動画のため最小マージン",
                ))
                result.success = True
            else:
                result.error = (
                    f"安全な使用区間が確保できません"
                    f"（動画長: {info.duration:.1f}秒）"
                )

        return result

    def _vision_analyze(
        self,
        video_path: str,
        info: VideoInfo,
    ) -> tuple[float, float, list[ClipRange], list[ClipRange]]:
        """Vision APIでフレームを解析してロゴ・クレジット位置を特定"""
        intro_skip = self.DEFAULT_INTRO_SKIP
        outro_skip = self.DEFAULT_OUTRO_SKIP
        logo_ranges: list[ClipRange] = []
        credit_ranges: list[ClipRange] = []

        # 冒頭のフレームをチェック（0秒, 2秒, 5秒, 8秒）
        intro_times = [t for t in [0, 2, 5, 8] if t < info.duration]
        last_logo_time = 0.0

        for t in intro_times:
            frame = extract_frame(video_path, t)
            if frame and self._has_logo_or_credit(frame):
                last_logo_time = t
                logo_ranges.append(ClipRange(
                    start_sec=max(0, t - 1), end_sec=t + 1,
                    reason=f"ロゴ検出 @ {t:.0f}秒",
                ))

        # ロゴが検出された最後の時間 + マージン
        if last_logo_time > 0:
            intro_skip = last_logo_time + 2.0

        # 末尾のフレームをチェック
        outro_times = [
            t for t in [
                info.duration - 2,
                info.duration - 5,
                info.duration - 8,
                info.duration - 12,
            ]
            if t > 0
        ]
        first_credit_time = info.duration

        for t in reversed(outro_times):
            frame = extract_frame(video_path, t)
            if frame and self._has_logo_or_credit(frame):
                first_credit_time = t
                credit_ranges.append(ClipRange(
                    start_sec=t - 1, end_sec=min(t + 1, info.duration),
                    reason=f"クレジット検出 @ {t:.0f}秒",
                ))

        # クレジットが検出された最初の時間 - マージン
        if first_credit_time < info.duration:
            outro_skip = info.duration - first_credit_time + 2.0

        return intro_skip, outro_skip, logo_ranges, credit_ranges

    def _has_logo_or_credit(self, frame: Image.Image) -> bool:
        """Vision APIでフレームにロゴ・クレジットがあるか判定"""
        if not self.client:
            return False

        # フレームを縮小してAPI送信
        max_size = 512
        w, h = frame.size
        if max(w, h) > max_size:
            ratio = max_size / max(w, h)
            frame = frame.resize(
                (int(w * ratio), int(h * ratio)), Image.LANCZOS
            )

        buf = io.BytesIO()
        frame.convert("RGB").save(buf, format="JPEG", quality=75)
        img_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
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
                        {
                            "type": "text",
                            "text": (
                                "このフレームにロゴ、タイトルテキスト、"
                                "クレジット（権利表記）、コピーライト表記が"
                                "含まれていますか？\n"
                                'JSONのみで回答: {"has_logo": true/false, '
                                '"has_credit": true/false, "description": "..."}'
                            ),
                        },
                    ],
                }],
            )

            text = response.content[0].text.strip()
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data.get("has_logo", False) or data.get("has_credit", False)
        except Exception:
            pass

        return False

    def clip_safe_ranges(
        self,
        video_path: str,
        result: ClipResult | None = None,
    ) -> list[str]:
        """安全な区間で動画をクリップして保存

        Args:
            video_path: 元動画パス
            result: 解析結果（Noneなら自動解析）

        Returns:
            生成されたクリップファイルパスのリスト
        """
        if result is None:
            result = self.analyze(video_path)

        if not result.success or not result.safe_ranges:
            print(f"  クリッピング不可: {result.error}")
            return []

        clip_paths: list[str] = []
        stem = Path(video_path).stem

        for i, clip_range in enumerate(result.safe_ranges):
            if not clip_range.is_valid(self.MIN_CLIP_DURATION):
                continue

            suffix = f"_clip{i}" if len(result.safe_ranges) > 1 else "_clipped"
            out_path = str(self.output_dir / f"{stem}{suffix}.mp4")

            print(f"  クリップ: {clip_range.start_sec:.1f}秒 → "
                  f"{clip_range.end_sec:.1f}秒 "
                  f"({clip_range.duration:.1f}秒)")

            if clip_video(video_path, out_path,
                         clip_range.start_sec, clip_range.end_sec):
                clip_paths.append(out_path)
                print(f"  保存: {out_path}")
            else:
                print(f"  クリップ失敗: {out_path}")

        return clip_paths

    def process_all(self, video_dir: str | Path) -> dict[str, ClipResult]:
        """ディレクトリ内の全シネマティック動画を処理

        Args:
            video_dir: 動画ディレクトリ

        Returns:
            {ファイルパス: ClipResult}
        """
        video_dir = Path(video_dir)
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
        results: dict[str, ClipResult] = {}

        videos = [
            p for p in sorted(video_dir.iterdir())
            if p.is_file() and p.suffix.lower() in video_exts
            and "_clip" not in p.stem and "_clipped" not in p.stem
        ]

        if not videos:
            print("  シネマティック動画が見つかりません")
            return results

        print(f"\n=== シネマティッククリッピング: {len(videos)}本 ===")

        for video in videos:
            print(f"\n  --- {video.name} ---")
            result = self.analyze(str(video))
            results[str(video)] = result

            if result.success:
                self.clip_safe_ranges(str(video), result)

        return results
