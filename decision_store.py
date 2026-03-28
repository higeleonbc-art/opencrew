"""判断履歴の永続化ストア（SQLite）

人間が確認・承認した判断を蓄積し、
将来的に同じパターンの判断を自動化するための基盤。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AssetDecision:
    """素材選択の判断レコード"""
    id: int = 0
    scene_context: str = ""       # "battle", "betrayal", "introduction" 等
    champion_name: str = ""
    asset_type: str = ""          # "splash", "cinematic", "irasutoya_composite"
    asset_path: str = ""
    irasutoya_path: str = ""      # irasutoya合成時の元画像
    confidence: float = 0.0
    confirmed: bool = False
    created_at: str = ""


@dataclass
class FacePosition:
    """いらすとや画像の顔位置レコード"""
    id: int = 0
    irasutoya_path: str = ""
    faces_json: str = "[]"        # [{x, y, width, height, label}]
    confirmed: bool = False
    created_at: str = ""

    @property
    def faces(self) -> list[dict]:
        return json.loads(self.faces_json)

    @faces.setter
    def faces(self, value: list[dict]) -> None:
        self.faces_json = json.dumps(value, ensure_ascii=False)


@dataclass
class LayoutCheck:
    """レイアウトQA結果レコード"""
    id: int = 0
    frame_description: str = ""   # どのシーンのフレームか
    issues_json: str = "[]"       # 発見された問題リスト
    adjustments_json: str = "{}"  # 適用された調整
    approved: bool = False
    created_at: str = ""


class DecisionStore:
    """SQLiteベースの判断履歴ストア"""

    def __init__(self, db_path: str | Path = "opencrew_decisions.db"):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS asset_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_context TEXT NOT NULL,
                champion_name TEXT NOT NULL DEFAULT '',
                asset_type TEXT NOT NULL,
                asset_path TEXT NOT NULL,
                irasutoya_path TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                confirmed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS face_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                irasutoya_path TEXT NOT NULL,
                faces_json TEXT NOT NULL DEFAULT '[]',
                confirmed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_face_path
                ON face_positions(irasutoya_path);

            CREATE TABLE IF NOT EXISTS layout_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                frame_description TEXT NOT NULL DEFAULT '',
                issues_json TEXT NOT NULL DEFAULT '[]',
                adjustments_json TEXT NOT NULL DEFAULT '{}',
                approved INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

    # --- 素材判断 ---

    def save_asset_decision(self, decision: AssetDecision) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO asset_decisions
               (scene_context, champion_name, asset_type, asset_path,
                irasutoya_path, confidence, confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (decision.scene_context, decision.champion_name,
             decision.asset_type, decision.asset_path,
             decision.irasutoya_path, decision.confidence,
             int(decision.confirmed)),
        )
        conn.commit()
        return cur.lastrowid or 0

    def confirm_asset_decision(self, decision_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE asset_decisions SET confirmed = 1 WHERE id = ?",
            (decision_id,),
        )
        conn.commit()

    def find_similar_decision(
        self, scene_context: str, champion_name: str = ""
    ) -> AssetDecision | None:
        """過去の確認済み判断から類似ケースを検索"""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM asset_decisions
               WHERE confirmed = 1
                 AND scene_context = ?
                 AND (champion_name = ? OR champion_name = '')
               ORDER BY created_at DESC LIMIT 1""",
            (scene_context, champion_name),
        ).fetchone()
        if row is None:
            return None
        return AssetDecision(
            id=row["id"],
            scene_context=row["scene_context"],
            champion_name=row["champion_name"],
            asset_type=row["asset_type"],
            asset_path=row["asset_path"],
            irasutoya_path=row["irasutoya_path"],
            confidence=row["confidence"],
            confirmed=bool(row["confirmed"]),
            created_at=row["created_at"],
        )

    def get_confirmed_count(self, scene_context: str) -> int:
        """特定コンテキストの確認済み判断数を返す"""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM asset_decisions
               WHERE confirmed = 1 AND scene_context = ?""",
            (scene_context,),
        ).fetchone()
        return row["cnt"] if row else 0

    # --- 顔位置 ---

    def save_face_positions(self, fp: FacePosition) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT OR REPLACE INTO face_positions
               (irasutoya_path, faces_json, confirmed)
               VALUES (?, ?, ?)""",
            (fp.irasutoya_path, fp.faces_json, int(fp.confirmed)),
        )
        conn.commit()
        return cur.lastrowid or 0

    def get_face_positions(self, irasutoya_path: str) -> FacePosition | None:
        """キャッシュ済みの顔位置を取得"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM face_positions WHERE irasutoya_path = ?",
            (irasutoya_path,),
        ).fetchone()
        if row is None:
            return None
        return FacePosition(
            id=row["id"],
            irasutoya_path=row["irasutoya_path"],
            faces_json=row["faces_json"],
            confirmed=bool(row["confirmed"]),
            created_at=row["created_at"],
        )

    def confirm_face_positions(self, irasutoya_path: str) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE face_positions SET confirmed = 1 WHERE irasutoya_path = ?",
            (irasutoya_path,),
        )
        conn.commit()

    # --- レイアウトチェック ---

    def save_layout_check(self, lc: LayoutCheck) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO layout_checks
               (frame_description, issues_json, adjustments_json, approved)
               VALUES (?, ?, ?, ?)""",
            (lc.frame_description, lc.issues_json,
             lc.adjustments_json, int(lc.approved)),
        )
        conn.commit()
        return cur.lastrowid or 0

    # --- ユーティリティ ---

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def stats(self) -> dict:
        """蓄積データの統計を返す"""
        conn = self._get_conn()
        asset_total = conn.execute(
            "SELECT COUNT(*) FROM asset_decisions"
        ).fetchone()[0]
        asset_confirmed = conn.execute(
            "SELECT COUNT(*) FROM asset_decisions WHERE confirmed = 1"
        ).fetchone()[0]
        face_total = conn.execute(
            "SELECT COUNT(*) FROM face_positions"
        ).fetchone()[0]
        face_confirmed = conn.execute(
            "SELECT COUNT(*) FROM face_positions WHERE confirmed = 1"
        ).fetchone()[0]
        return {
            "asset_decisions": {"total": asset_total, "confirmed": asset_confirmed},
            "face_positions": {"total": face_total, "confirmed": face_confirmed},
        }
