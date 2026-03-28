"""OpenCrew パイプラインオーケストレーター

台本JSONから動画出力までの全工程を統合管理する。

フロー:
1. 台本解析 → チャンピオン名・場面コンテキスト抽出
2. 素材検索 → 各セリフに対して素材候補を取得
3. 素材選択 → AI提案 + 人間確認（初期）/ 自動（学習後）
4. いらすとや合成 → Vision APIで顔検出 → アイコン差し替え
5. レイアウトQA → プレビューフレームをVision APIでチェック
6. 不足素材通知 → デスクトップ通知
7. 台本JSONにassetフィールドを追加 → 既存process_script()へ渡す
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .asset_finder import AssetFinder, AssetMatch
from .decision_store import AssetDecision, DecisionStore
from .face_compositor import CompositeRequest, FaceCompositor
from .layout_checker import LayoutChecker
from .notifier import Notifier
from .script_analyzer import ScriptAnalysis, SceneLine, analyze_script, analyze_script_with_ai


@dataclass
class LineAssetAssignment:
    """各セリフへの素材割り当て"""
    line_index: int
    asset_type: str              # "splash", "cinematic", "irasutoya_composite"
    asset_path: str = ""         # 最終的な素材パス
    splash_bg_path: str = ""     # 背景スプラッシュアート
    irasutoya_path: str = ""     # いらすとや元画像（合成時）
    icon_paths: list[str] = field(default_factory=list)  # 顔差し替え用アイコン
    champion_names: list[str] = field(default_factory=list)
    confirmed: bool = False
    auto_decided: bool = False


@dataclass
class PipelineResult:
    """パイプラインの実行結果"""
    enriched_script: dict = field(default_factory=dict)
    assignments: list[LineAssetAssignment] = field(default_factory=list)
    missing_assets: list[str] = field(default_factory=list)
    composite_images: dict[int, str] = field(default_factory=dict)  # line_index → 合成画像パス
    success: bool = False


class OpenCrewPipeline:
    """AI駆動の動画素材自動選択パイプライン"""

    def __init__(self, config: dict):
        """
        Args:
            config: config.yamlの全体dict（opencrew セクションを含む）
        """
        oc_config = config.get("opencrew", {})
        self.enabled = oc_config.get("enabled", False)
        self.mode = oc_config.get("mode", "confirmation")
        self.auto_threshold = oc_config.get("auto_confidence_threshold", 3)
        self.layout_qa_enabled = oc_config.get("layout_qa", False)

        # コンポーネント初期化
        self.client: anthropic.Anthropic | None = None
        try:
            self.client = anthropic.Anthropic()
        except Exception:
            pass

        # 素材ディレクトリ
        asset_dirs = oc_config.get("asset_dirs", {})
        config_dir = Path(config.get("_config_dir", "."))
        resolved_dirs = {}
        for key, path in asset_dirs.items():
            p = Path(path)
            if not p.is_absolute():
                p = config_dir / p
            resolved_dirs[key] = str(p)

        self.finder = AssetFinder(resolved_dirs)
        self.store = DecisionStore(
            oc_config.get("db_path", "opencrew_decisions.db")
        )
        self.notifier = Notifier(
            enabled=oc_config.get("notifications", True)
        )
        self.compositor = FaceCompositor(
            client=self.client,
            store=self.store,
        ) if self.client else None
        self.layout_checker = LayoutChecker(
            client=self.client
        ) if self.client and self.layout_qa_enabled else None

        # 合成画像の一時保存先
        self._temp_dir = tempfile.mkdtemp(prefix="opencrew_")

    def process(self, script: dict) -> PipelineResult:
        """メインエントリポイント: 台本を解析し、素材を割り当てて返す

        Args:
            script: 台本JSON全体

        Returns:
            PipelineResult: 素材割り当て済みの結果
        """
        result = PipelineResult()

        # Step 1: 台本解析
        print("\n=== OpenCrew: 台本解析 ===")
        if self.client:
            analysis = analyze_script_with_ai(script, self.client)
        else:
            analysis = analyze_script(script)

        print(f"  タイトル: {analysis.title}")
        print(f"  メインチャンピオン: {', '.join(analysis.main_champions)}")
        print(f"  全登場チャンピオン: {', '.join(analysis.all_champions)}")
        print(f"  セリフ数: {analysis.total_lines}")

        # Step 2: 素材検索 & 不足チェック
        print("\n=== OpenCrew: 素材検索 ===")
        all_assets: dict[str, dict[str, list[AssetMatch]]] = {}
        for champ in analysis.all_champions:
            assets = self.finder.find_all_for_champion(champ)
            all_assets[champ] = assets
            splash_count = len(assets["splash"])
            cine_count = len(assets["cinematic"])
            icon_count = len(assets["icon"])
            print(f"  {champ}: スプラッシュ={splash_count}, "
                  f"シネマティック={cine_count}, アイコン={icon_count}")

        # 不足素材チェック
        need_cinematic = any(
            l.suggested_asset_type == "cinematic" for l in analysis.lines
        )
        missing = self.finder.check_missing(
            analysis.all_champions, need_cinematic=need_cinematic
        )

        # いらすとや素材の確認
        irasutoya_lines = [
            l for l in analysis.lines
            if l.suggested_asset_type == "irasutoya_composite"
        ]
        if irasutoya_lines:
            available_irasutoya = self.finder.list_available_irasutoya()
            if not available_irasutoya:
                missing.append("いらすとや素材（1つ以上必要）")

        if missing:
            result.missing_assets = missing
            self.notifier.notify_missing_assets(missing)

        # Step 3: 各セリフへの素材割り当て
        print("\n=== OpenCrew: 素材割り当て ===")
        assignments = self._assign_assets(analysis, all_assets)
        result.assignments = assignments

        # Step 4: いらすとや合成
        if self.compositor:
            print("\n=== OpenCrew: いらすとや合成 ===")
            self._process_composites(assignments, result)

        # Step 5: 台本JSONにassetフィールドを追加
        result.enriched_script = self._enrich_script(
            script, assignments, result.composite_images
        )
        result.success = True

        # Step 6: レイアウトQA（オプション）
        if self.layout_checker and self.layout_qa_enabled:
            print("\n=== OpenCrew: レイアウトQA ===")
            # ここではフレームレンダリング後に別途呼び出す想定
            print("  レイアウトQAは動画レンダリング後に実行されます")

        # 統計表示
        stats = self.store.stats()
        print(f"\n=== OpenCrew: 完了 ===")
        print(f"  蓄積判断数: {stats['asset_decisions']['confirmed']} 確認済み")
        print(f"  顔位置キャッシュ: {stats['face_positions']['confirmed']} 確認済み")

        return result

    def _assign_assets(
        self,
        analysis: ScriptAnalysis,
        all_assets: dict[str, dict[str, list[AssetMatch]]],
    ) -> list[LineAssetAssignment]:
        """各セリフに素材を割り当て"""
        assignments: list[LineAssetAssignment] = []
        main_champ = analysis.main_champions[0] if analysis.main_champions else ""

        for line in analysis.lines:
            assignment = LineAssetAssignment(
                line_index=line.index,
                asset_type=line.suggested_asset_type,
                champion_names=line.champions_mentioned or [main_champ],
            )

            # メインチャンピオンの _0 スプラッシュを背景として常に設定
            if main_champ:
                default_splash = self.finder.find_splash_default(main_champ)
                if default_splash:
                    assignment.splash_bg_path = default_splash.path

            # アセットタイプに応じた素材選択
            if line.suggested_asset_type == "splash":
                assignment.asset_path = assignment.splash_bg_path

            elif line.suggested_asset_type == "cinematic":
                # シネマティック動画はチャンピオン名で検索しない（手動配置）
                # AIが動画内容から使える箇所を判断する
                cine_list = self.finder.find_cinematic()
                if cine_list:
                    assignment.asset_path = cine_list[0].path
                else:
                    # シネマティックがなければスプラッシュにフォールバック
                    assignment.asset_type = "splash"
                    assignment.asset_path = assignment.splash_bg_path

            elif line.suggested_asset_type == "irasutoya_composite":
                # いらすとや素材を検索
                keyword = line.suggested_irasutoya_keyword
                irasutoya_matches = self.finder.find_irasutoya(keyword)
                if irasutoya_matches:
                    assignment.irasutoya_path = irasutoya_matches[0].path
                    # デフォルトアイコン（_0）を使用
                    for champ in (line.champions_mentioned or [main_champ]):
                        icon = self.finder.find_icon_default(champ)
                        if icon:
                            assignment.icon_paths.append(icon.path)
                else:
                    # いらすとやがなければスプラッシュにフォールバック
                    assignment.asset_type = "splash"
                    assignment.asset_path = assignment.splash_bg_path

            # 確認モードでの判断
            auto_decided = self._check_auto_decide(line, assignment)
            assignment.auto_decided = auto_decided

            if not auto_decided and self.mode == "confirmation":
                assignment.confirmed = self._confirm_assignment(line, assignment)
            else:
                assignment.confirmed = True

            # 判断を保存
            if assignment.confirmed and assignment.asset_path:
                self.store.save_asset_decision(AssetDecision(
                    scene_context=line.scene_context,
                    champion_name=main_champ,
                    asset_type=assignment.asset_type,
                    asset_path=assignment.asset_path,
                    irasutoya_path=assignment.irasutoya_path,
                    confidence=1.0 if auto_decided else 0.8,
                    confirmed=True,
                ))

            assignments.append(assignment)

        return assignments

    def _check_auto_decide(
        self, line: SceneLine, assignment: LineAssetAssignment
    ) -> bool:
        """過去の判断データから自動決定可能か判定"""
        if self.mode != "auto":
            return False

        count = self.store.get_confirmed_count(line.scene_context)
        return count >= self.auto_threshold

    def _confirm_assignment(
        self, line: SceneLine, assignment: LineAssetAssignment
    ) -> bool:
        """人間に素材割り当てを確認してもらう"""
        print(f"\n  --- セリフ {line.index + 1}/{line.text[:30]}... ---")
        print(f"  場面: {line.scene_context}")
        print(f"  提案: {assignment.asset_type}")
        if assignment.asset_path:
            print(f"  素材: {Path(assignment.asset_path).name}")
        if assignment.irasutoya_path:
            print(f"  いらすとや: {Path(assignment.irasutoya_path).name}")
            print(f"  アイコン: {[Path(p).name for p in assignment.icon_paths]}")

        result = self.notifier.prompt_confirm(
            "この素材割り当てを承認しますか？",
            default="y",
        )
        return result == "y"

    def _process_composites(
        self,
        assignments: list[LineAssetAssignment],
        result: PipelineResult,
    ) -> None:
        """いらすとや合成を実行"""
        composite_assignments = [
            a for a in assignments
            if a.asset_type == "irasutoya_composite"
            and a.irasutoya_path
            and a.icon_paths
            and a.confirmed
        ]

        if not composite_assignments:
            print("  合成対象なし")
            return

        for assignment in composite_assignments:
            print(f"  合成中: セリフ{assignment.line_index + 1} "
                  f"({Path(assignment.irasutoya_path).name})")

            request = CompositeRequest(
                irasutoya_path=assignment.irasutoya_path,
                icon_paths=assignment.icon_paths,
                champion_names=assignment.champion_names,
            )

            comp_result = self.compositor.composite(request)

            if comp_result.success and comp_result.image:
                # 合成画像を一時ファイルに保存
                out_path = os.path.join(
                    self._temp_dir,
                    f"composite_line{assignment.line_index}.png",
                )
                comp_result.image.save(out_path)
                result.composite_images[assignment.line_index] = out_path
                assignment.asset_path = out_path
                print(f"    完了: {out_path}")

                # 確認モード: プレビューを見せる
                if self.mode == "confirmation":
                    preview = self.compositor.generate_preview(request)
                    if preview:
                        preview_path = os.path.join(
                            self._temp_dir,
                            f"preview_line{assignment.line_index}.png",
                        )
                        preview.save(preview_path)

                        approved = self.notifier.show_preview_and_confirm(
                            f"セリフ{assignment.line_index + 1}の合成結果",
                            preview_path,
                        )
                        if approved and self.compositor.store:
                            self.compositor.store.confirm_face_positions(
                                assignment.irasutoya_path
                            )
                        elif not approved:
                            print("    合成を却下。スプラッシュアートにフォールバック")
                            assignment.asset_type = "splash"
                            assignment.asset_path = assignment.splash_bg_path
            else:
                print(f"    合成失敗: {comp_result.error}")
                assignment.asset_type = "splash"
                assignment.asset_path = assignment.splash_bg_path

    def _enrich_script(
        self,
        original_script: dict,
        assignments: list[LineAssetAssignment],
        composite_images: dict[int, str],
    ) -> dict:
        """台本JSONにassetフィールドを追加

        既存のprocess_script()がline.get("asset")で素材パスを読むので、
        そのフォーマットに合わせて追加する。
        """
        enriched = copy.deepcopy(original_script)
        sd = enriched.get("scriptData", enriched)
        lines = sd.get("lines", [])

        for assignment in assignments:
            idx = assignment.line_index
            if idx < len(lines):
                asset_path = assignment.asset_path
                if not asset_path and assignment.splash_bg_path:
                    asset_path = assignment.splash_bg_path

                if asset_path:
                    lines[idx]["asset"] = asset_path

        return enriched

    def check_layout(
        self,
        frame_image,
        context: str = "",
        line_text: str = "",
    ):
        """レンダリング後のフレームをレイアウトチェック"""
        if not self.layout_checker:
            return None
        return self.layout_checker.check_frame(frame_image, context, line_text)

    def close(self) -> None:
        """リソース解放"""
        self.store.close()
