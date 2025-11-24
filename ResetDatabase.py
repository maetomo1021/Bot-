import os
import sqlite3

# このファイル(ResetDatabase.py)と同じ階層の casino.db を対象にする
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "casino.db")

def reset_db():
    if os.path.exists(DB_PATH):
        print(f"[INFO] 既存のデータベースを削除します: {DB_PATH}")
        os.remove(DB_PATH)
    else:
        print(f"[INFO] データベースファイルが存在しません: {DB_PATH}")

    # 必要ならここで新規作成もできるが、
    # maetomo.py 起動時に init_db() が自動で作り直すので削除だけでOK
    print("[INFO] リセット完了。次回 maetomo.py 実行時に新しいDBが作成されます。")

if __name__ == "__main__":
    print("=== casino.db リセットツール ===")
    print(f"対象ファイル: {DB_PATH}")
    ans = input("本当にリセットしますか？ (y/N): ").strip().lower()
    if ans == "y":
        reset_db()
    else:
        print("キャンセルしました。")
