"""台本に基づくいらすとや素材一括ダウンロード

既存のirasutoya_downloader.pyを使用して、
FF14パッチ7.2台本に必要な素材をダウンロードする。

【使い方】
  pip install requests beautifulsoup4
  python download_for_script.py
"""

from pathlib import Path
from irasutoya_downloader import IrasutoyaDownloader


def main():
    save_dir = Path(__file__).parent / "opencrew_assets" / "irasutoya"

    downloader = IrasutoyaDownloader(save_dir=str(save_dir))

    # FF14パッチ7.2新コンテンツまとめ + ゆっくり実況リアクション系
    # 合計18キーワード × 各1枚 = 最大18枚（< 20枚制限）
    context_keywords = {
        # ゲーム・PC関連
        "gaming_pc": "ゲーミングPC",
        "game_screen": "ゲーム画面",
        "voice_chat": "ボイスチャット ゲーム",
        # リアクション系
        "surprise": "驚く 男性",
        "surprise_eye": "目が飛び出る 驚き",
        "discovery": "発見 驚く",
        "happy": "喜ぶ 嬉しい",
        "thinking": "考える 悩む",
        "angry": "怒る",
        "cry": "泣く 悲しい",
        # ファンタジー・RPG（FF14テーマ）
        "hero": "勇者",
        "magic": "魔法使い",
        "sword": "剣",
        "rpg_party": "RPG パーティ",
        "dungeon": "ダンジョン 洞窟",
        # ニュース・解説系
        "news": "ニュース 速報",
        "checklist": "チェックリスト まとめ",
        "update": "アップデート",
    }

    print(f"対象: {len(context_keywords)}キーワード（各1枚）")
    print(f"保存先: {save_dir}")
    print()

    results = downloader.download_for_contexts(context_keywords)
    print("\n" + downloader.get_usage_report())

    total = sum(len(items) for items in results.values())
    print(f"\n合計ダウンロード数: {total}")


if __name__ == "__main__":
    main()
