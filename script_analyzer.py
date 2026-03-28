"""台本JSON解析モジュール

台本からチャンピオン名・場面コンテキスト・登場人物関係を抽出する。
Claude APIを使って場面の雰囲気を判定し、適切な素材タイプを提案する。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import anthropic


# チャンピオン名の日英マッピング（必要に応じて拡張）
CHAMPION_NAME_MAP: dict[str, str] = {
    "アッシュ": "Ashe",
    "ヤスオ": "Yasuo",
    "シェン": "Shen",
    "ゼド": "Zed",
    "アーリ": "Ahri",
    "ガレン": "Garen",
    "ラックス": "Lux",
    "ジンクス": "Jinx",
    "ケイトリン": "Caitlyn",
    "ヴァイ": "Vi",
    "エコー": "Ekko",
    "グウェン": "Gwen",
    "ヴィエゴ": "Viego",
    "セナ": "Senna",
    "ルシアン": "Lucian",
    "サイラス": "Sylas",
    "ダリウス": "Darius",
    "ドレイヴン": "Draven",
    "カタリナ": "Katarina",
    "リー・シン": "LeeSin",
    "アカリ": "Akali",
    "ケイン": "Kayn",
    "ラカン": "Rakan",
    "ザヤ": "Xayah",
    "セト": "Sett",
    "モルデカイザー": "Mordekaiser",
    "ヨネ": "Yone",
    "リリア": "Lillia",
    "サミーラ": "Samira",
    "レル": "Rell",
    "ヴェックス": "Vex",
    "ザイラ": "Zyra",
    "アイバーン": "Ivern",
    "オレリオン・ソル": "AurelionSol",
    "キンドレッド": "Kindred",
}

# 場面コンテキストのキーワードマッピング
SCENE_KEYWORDS: dict[str, list[str]] = {
    "battle": ["戦", "襲撃", "攻撃", "倒", "殺", "激突", "対決", "斬", "戦闘"],
    "betrayal": ["裏切", "離れ", "失望", "敵", "去っ", "反逆"],
    "sadness": ["悲し", "涙", "失", "死", "亡", "別れ", "重い"],
    "introduction": ["紹介", "今回は", "1分紹介", "1分解説"],
    "friendship": ["友", "仲", "兄弟", "共に", "一緒", "絆"],
    "training": ["修行", "鍛", "育", "修練"],
    "resolution": ["決意", "継ぎ", "守り", "選び", "背負", "再建", "使命"],
    "call_to_action": ["チャンネル登録", "よろしく", "コメント", "いいね"],
}


@dataclass
class SceneLine:
    """解析済みの台本1行"""
    index: int
    speaker: str
    text: str
    champions_mentioned: list[str] = field(default_factory=list)
    scene_context: str = "general"           # battle, betrayal, etc.
    suggested_asset_type: str = "splash"     # splash, cinematic, irasutoya_composite
    suggested_irasutoya_keyword: str = ""    # いらすとや素材の検索キーワード


@dataclass
class ScriptAnalysis:
    """台本全体の解析結果"""
    title: str = ""
    subtitle: str = ""
    main_champions: list[str] = field(default_factory=list)  # メインチャンピオン
    all_champions: list[str] = field(default_factory=list)   # 全登場チャンピオン
    lines: list[SceneLine] = field(default_factory=list)
    total_lines: int = 0


def extract_champions_from_text(text: str) -> list[str]:
    """テキストからチャンピオン名を抽出"""
    found = []
    for jp_name in CHAMPION_NAME_MAP:
        if jp_name in text:
            found.append(jp_name)
    return found


def detect_scene_context(text: str) -> str:
    """テキストから場面コンテキストをキーワードで推定"""
    scores: dict[str, int] = {}
    for context, keywords in SCENE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[context] = score
    if not scores:
        return "general"
    return max(scores, key=scores.get)


def suggest_asset_type(scene_context: str, line_index: int, total_lines: int) -> str:
    """場面コンテキストからアセットタイプを提案

    ルール:
    - introduction / call_to_action → splash（シンプルに）
    - battle / betrayal → cinematic（動きのあるシーン）
    - friendship / training / sadness → irasutoya_composite（感情表現）
    - resolution → splash（厳かに）
    - 冒頭と末尾 → splash（安定感）
    """
    if line_index <= 1 or line_index >= total_lines - 2:
        return "splash"

    cinematic_contexts = {"battle", "betrayal"}
    composite_contexts = {"friendship", "training", "sadness"}

    if scene_context in cinematic_contexts:
        return "cinematic"
    if scene_context in composite_contexts:
        return "irasutoya_composite"
    return "splash"


def suggest_irasutoya_keyword(scene_context: str, text: str) -> str:
    """場面に適したいらすとや検索キーワードを提案"""
    keyword_map = {
        "battle": "戦い",
        "betrayal": "裏切り",
        "sadness": "泣く",
        "friendship": "友達",
        "training": "修行",
        "resolution": "決意",
    }
    return keyword_map.get(scene_context, "")


def analyze_script(script_data: dict) -> ScriptAnalysis:
    """台本JSONを解析してチャンピオン名・場面コンテキストを抽出

    Args:
        script_data: 台本JSON全体（mainTweet, scriptDataを含む）

    Returns:
        ScriptAnalysis: 解析結果
    """
    sd = script_data.get("scriptData", script_data)
    title = sd.get("title", "")
    lines_data = sd.get("lines", [])
    total_lines = len(lines_data)

    # タイトルからメインチャンピオンを抽出
    main_champions = extract_champions_from_text(title)

    # サブタイトル（" - "区切りの後半、または description の先頭）
    subtitle = sd.get("description", "")
    if " - " in title:
        parts = title.split(" - ", 1)
        title = parts[0]
        subtitle = parts[1]

    # 全行を解析
    all_champions_set: set[str] = set(main_champions)
    scene_lines: list[SceneLine] = []

    for i, line in enumerate(lines_data):
        text = line.get("text", "")
        speaker = line.get("speaker", "")

        champions = extract_champions_from_text(text)
        all_champions_set.update(champions)

        context = detect_scene_context(text)
        asset_type = suggest_asset_type(context, i, total_lines)
        irasutoya_kw = suggest_irasutoya_keyword(context, text)

        scene_lines.append(SceneLine(
            index=i,
            speaker=speaker,
            text=text,
            champions_mentioned=champions,
            scene_context=context,
            suggested_asset_type=asset_type,
            suggested_irasutoya_keyword=irasutoya_kw,
        ))

    return ScriptAnalysis(
        title=title,
        subtitle=subtitle,
        main_champions=main_champions,
        all_champions=sorted(all_champions_set),
        lines=scene_lines,
        total_lines=total_lines,
    )


def analyze_script_with_ai(
    script_data: dict,
    client: anthropic.Anthropic | None = None,
) -> ScriptAnalysis:
    """Claude APIを使ってより精度の高い場面解析を行う

    キーワードベースの解析結果をAIで補正・拡充する。
    APIが使えない場合はキーワードベースの結果をそのまま返す。
    """
    # まずキーワードベースで解析
    analysis = analyze_script(script_data)

    if client is None:
        try:
            client = anthropic.Anthropic()
        except Exception:
            return analysis

    sd = script_data.get("scriptData", script_data)
    lines_data = sd.get("lines", [])

    # AIに場面解析を依頼
    lines_text = "\n".join(
        f"[{i}] {line.get('speaker', '')}: {line.get('text', '')}"
        for i, line in enumerate(lines_data)
    )

    prompt = f"""以下はLoLチャンピオン紹介動画の台本です。各セリフの「場面の雰囲気」を判定してください。

台本:
{lines_text}

各セリフについて、以下のJSON配列で返してください（説明文は不要、JSONのみ）:
[
  {{"index": 0, "context": "introduction", "asset_type": "splash", "irasutoya_keyword": ""}},
  ...
]

context の選択肢: introduction, battle, betrayal, sadness, friendship, training, resolution, call_to_action, general
asset_type の選択肢:
- "splash" = スプラッシュアートのみ（導入、締め、厳かな場面）
- "cinematic" = シネマティック動画（戦闘、ドラマチックな場面）
- "irasutoya_composite" = いらすとや＋チャンピオンアイコン合成（感情表現、日常的な場面）

irasutoya_keyword: irasutoya_compositeの場合のみ、適切ないらすとや素材の検索キーワード（日本語1-2語）"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        # JSONブロックを抽出
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            ai_results = json.loads(json_match.group())
            # AI結果で補正
            for item in ai_results:
                idx = item.get("index", -1)
                if 0 <= idx < len(analysis.lines):
                    line = analysis.lines[idx]
                    line.scene_context = item.get("context", line.scene_context)
                    line.suggested_asset_type = item.get(
                        "asset_type", line.suggested_asset_type
                    )
                    line.suggested_irasutoya_keyword = item.get(
                        "irasutoya_keyword", line.suggested_irasutoya_keyword
                    )
    except Exception:
        pass  # AI解析失敗時はキーワードベースの結果を使用

    return analysis
