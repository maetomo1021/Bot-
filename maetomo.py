import os
import asyncio
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from aiohttp import web
import json
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import matplotlib.pyplot as plt
from dataclasses import dataclass
from matplotlib.ticker import MaxNLocator

# 日本語フォント設定（どれかPCに入ってるもの）
plt.rcParams["font.family"] = "Yu Gothic"  # or "MS Gothic", "Meiryo"
plt.rcParams["axes.unicode_minus"] = False  # マイナス記号の文字化け防止
import requests

@dataclass
class SlotHit:
    player: str
    slot_key: str
    slot_name: str
    hit_type: str
    win_amount: int
    raw_message: str
# from bs4 import BeautifulSoup

# =====================================================
#  パス設定 & .env 読み込み
# =====================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise RuntimeError("TOKEN が .env から読み込めていません。'.env' に TOKEN=xxxxx を書いてください。")

# =====================================================
#  設定（ここだけ変えればOK）
# =====================================================

# VC監視用（まだ未使用なら None のまま）
VOICE_CHANNEL_ID: Optional[int] = None

# Casinoログリアルタイム監視を流すチャンネル
CASINO_CHANNEL_ID: int = 1442032261792006214

# 日次集計結果を貼るチャンネル
DAILY_REPORT_CHANNEL_ID: int = 1442034958007799919

# Minecraft latest.log のパス
MC_LOG_PATH = r"C:\Users\Maeda\AppData\Roaming\.minecraft\logs\latest.log"

# カジノ関連ログのキーワード
CASINO_KEYWORDS = [
    "BIGBONUS",
    "RUSH",
    "スロット",
    "ZEUSGAME",
    "突入",
    "チャンス",
    "獲得",
    "残基",
    "ラッシュ",
    "ゲット",
    "止まった",
    "winnning",
    "ジャック",
    "当たった",
    "残基",
    "増えた",
    "到達",
    "段目",
    "雷"
]

# SQLite DB のパス
DB_PATH = os.path.join(BASE_DIR, "casino.db")

# =====================================================
#  SQLite 初期化
# =====================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 生ログ用テーブル（今まで通り）
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,          -- 保存した日時 (ISO)
            date TEXT,        -- YYYY-MM-DD
            player TEXT,      -- プレイヤー名
            slot TEXT,        -- スロット名
            result TEXT,      -- 結果 (BIGBONUS / 100回突入 など)
            raw_message TEXT  -- 生ログ
        )
        """
    )

    # ★ 新規：スロットごとの「当たり」だけを記録するテーブル
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS slot_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,          -- 当たり検出した時間 (ISO)
            date TEXT,        -- YYYY-MM-DD
            player TEXT,      -- プレイヤー名
            slot_key TEXT,    -- 論理名: TRAIN / TENKU / ZEUS / ABYSS / NATSU / ...
            slot_name TEXT,   -- 表示名: 列車スロット / 天空スロット / アビススロット 等
            hit_type TEXT,    -- 当たりの種類: BIGBONUS / RUSH / GALAXY_RUSH / 深淵ヒット など
            win_amount INTEGER, -- 獲得金額 (わからなければ 0)
            raw_message TEXT  -- 当たり判定に使った生ログ
        )
        """
    )
        # ★ 新規：ルーレットの「獲得」だけを記録するテーブル
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS roulette_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,          -- 獲得を検出した時間 (ISO)
            date TEXT,        -- YYYY-MM-DD
            player TEXT,      -- プレイヤー名
            game TEXT,        -- ゲーム名（例: Roulette）
            amount INTEGER,   -- 獲得金額
            raw_message TEXT  -- 生ログ
        )
        """
    )


    conn.commit()
    conn.close()


init_db()

# =====================================================
#  discord.py 設定
# =====================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True  # Developer Portal 側でも ON

bot = commands.Bot(command_prefix="!", intents=intents)

is_voice_logging: bool = False
is_casino_logging: bool = False
casino_task: Optional[asyncio.Task] = None
last_casino_message: Optional[str] = None  # ★ 追加
# =====================================================
#  Casino メッセージ解析 & DB 保存
# =====================================================


def parse_casino_message(msg: str) -> Optional[Tuple[str, str, str]]:
    """
    カジノ関連メッセージから (player, slot, result) を取り出す。
    マッチしなければ None。
    必要に応じてここにパターン追加していく。
    """

    # ① BIGBONUS パターン
    # 例: donbee_kituneがカッピースロットでBIGBONUS！ハッピー！
    m = re.search(
        r"(?P<player>\S+)が(?P<slot>.+?)で(?P<result>BIGBONUS)[！!？\s]*.*",
        msg
    )
    if m:
        d = m.groupdict()
        return d["player"], d["slot"], d["result"]

    # ② ZEUSGAME 突入パターン
    # 例: Shuri_wakuが100回のZEUSGAMEに突入！
    m = re.search(
        r"(?P<player>\S+)が(?P<times>\d+)回の(?P<slot>\S+)に突入[！!]",
        msg
    )
    if m:
        d = m.groupdict()
        result = f"{d['times']}回突入"
        return d["player"], d["slot"], result


    # ④ チャンス状態突入
    # 例: maetomo1021がカッピースロットでチャンスに突入！
    m = re.search(
        r"(?P<player>\S+)が(?P<slot>.+?)で(?P<state>チャンス|CZ|前兆)に突入[！!]",
        msg
    )
    if m:
        d = m.groupdict()
        result = f"{d['state']}突入"
        return d["player"], d["slot"], result

    # ⑤ RUSH / ラッシュ 突入
    # 例: maetomo1021がカッピースロットでRUSHに突入！
    m = re.search(
        r"(?P<player>\S+)が(?P<slot>.+?)で(?P<state>RUSH|ラッシュ)に突入[！!]",
        msg
    )
    if m:
        d = m.groupdict()
        result = f"{d['state']}突入"
        return d["player"], d["slot"], result

    # TODO: 他にも「〇〇ゲーム開始」「〇〇終了」など欲しくなったらここに足す

    return None



def save_spin(player: str, slot: str, result: str, raw: str) -> None:
    """解析した結果を SQLite に保存"""
    now = datetime.now()  # PC ローカル時間（JST前提）
    ts = now.isoformat(timespec="seconds")
    date_str = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO spins (ts, date, player, slot, result, raw_message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts, date_str, player, slot, result, raw),
    )
    conn.commit()
    conn.close()

def save_roulette_win(player: str, amount: int, raw: str) -> None:
    """
    ルーレットでの「○○円獲得」を roulette_hits に保存する。
    """
    now = datetime.now()
    ts = now.isoformat(timespec="seconds")
    date_str = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO roulette_hits (ts, date, player, game, amount, raw_message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ts, date_str, player, "Roulette", amount, raw),
    )
    conn.commit()
    conn.close()


def parse_roulette_message(msg: str) -> Optional[Tuple[str, int]]:
    """
    ルーレットの「○○円獲得」メッセージを検出して (player, amount) を返す。
    ログ形式に合わせて正規表現はあとで調整していく前提。

    想定例:
      maetomo1021がルーレットで50000円獲得！
    """
    # ルーレット系のメッセージだけを見る簡易フィルタ
    if ("ルーレット" not in msg) and ("roulette" not in msg.lower()):
        return None

    # プレイヤー名 + 金額をざっくり抜く
    m = re.search(r"(?P<player>\S+).+?([+-]?[0-9,]+)円獲得", msg)
    if not m:
        return None

    player = m.group("player")
    amount_str = m.group(2)
    try:
        amount = int(amount_str.replace(",", ""))
    except ValueError:
        return None

    return player, amount

def save_slot_hit(hit: SlotHit) -> None:
    """
    スロットごとの「当たり1回」を slot_hits テーブルに保存する。
    ※ SlotHit はファイル先頭で定義済みの dataclass を利用。
    """
    now = datetime.now()
    ts = now.isoformat(timespec="seconds")
    date_str = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO slot_hits
        (ts, date, player, slot_key, slot_name, hit_type, win_amount, raw_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            date_str,
            hit.player,
            hit.slot_key,
            hit.slot_name,
            hit.hit_type,
            hit.win_amount,
            hit.raw_message,
        ),
    )
    conn.commit()
    conn.close()


def detect_and_save_slot_hits(msg: str, parsed: Optional[Tuple[str, str, str]] = None) -> None:
    """
    1行のログから「スロットの当たり」を検出して、
    見つかった分だけ slot_hits に保存する。

    ※ スロットごとに仕様が違うので、ここにブロック分けしてロジックを追加していく。
    """
    hits: List[SlotHit] = []

    # 解析済み (player, slot, result) があれば使う。なければここで解析。
    if parsed is None:
        parsed = parse_casino_message(msg)

    player_from_parsed: Optional[str] = None
    slot_from_parsed: Optional[str] = None
    result_from_parsed: Optional[str] = None

    if parsed is not None:
        player_from_parsed, slot_from_parsed, result_from_parsed = parsed

    # =============================
    #  列車スロット（山手線スロなど）
    #  仕様案：
    #    - 「お疲れ様！」が出たら1プレイ分を1カウント
    #    - もう1周継続が来たら、当たり回数に +1 する 等
    #  → 実際のログメッセージを見ながら、ここに正規表現を追加していく。
    # =============================
    # if "山手線スロ" in msg and "お疲れ様！" in msg:
    #     hits.append(SlotHit(
    #         player=player_from_parsed or "UNKNOWN",
    #         slot_key="TRAIN",
    #         slot_name="列車スロット",
    #         hit_type="FINISH",
    #         win_amount=0,
    #         raw_message=msg
    #     ))

    # =============================
    #  天空スロット
    #  仕様案：
    #    - 4段階目の後にRUSHが当たったら1回加算
    #    - 「獲得！」的なメッセージが来たら終了1カウント
    # =============================
    # if "天空スロット" in msg and "RUSH" in msg:
    #     hits.append(SlotHit(
    #         player=player_from_parsed or "UNKNOWN",
    #         slot_key="TENKU",
    #         slot_name="天空スロット",
    #         hit_type="RUSH",
    #         win_amount=0,
    #         raw_message=msg
    #     ))

    # =============================
    #  アビススロット
    #  「深淵を」で当たり
    # =============================
    if "アビス" in msg and "深淵を" in msg:
        hits.append(SlotHit(
            player=player_from_parsed or "UNKNOWN",
            slot_key="ABYSS",
            slot_name="アビススロット",
            hit_type="DEEP_HIT",
            win_amount=0,
            raw_message=msg
        ))

    # =============================
    #  夏の大三角スロット
    #  「GalaxyRush」で当たり
    # =============================
    if "夏の大三角" in msg and "GalaxyRush" in msg:
        hits.append(SlotHit(
            player=player_from_parsed or "UNKNOWN",
            slot_key="NATSU",
            slot_name="夏の大三角スロット",
            hit_type="GALAXY_RUSH",
            win_amount=0,
            raw_message=msg
        ))

    # =============================
    #  ZEUS / ぜうす / はーです スロット系
    #  とりあえず BIGBONUS / RUSH 系をヒット扱い
    # =============================
    if (("ZEUS" in msg) or ("ぜうす" in msg) or ("はーです" in msg)) and ("BIGBONUS" in msg):
        hits.append(SlotHit(
            player=player_from_parsed or "UNKNOWN",
            slot_key="ZEUS_FAMILY",
            slot_name="ゼウス系スロット",
            hit_type="BIGBONUS",
            win_amount=0,
            raw_message=msg
        ))

    # TODO:
    #  - ZEUS / ぜうす / はーです / 夏の大三角 / 列車 / 天空スロット の
    #    本当の仕様に合わせて正規表現をここに追加していく。

    # ここまでで集まった hits をDBに保存
    for h in hits:
        save_slot_hit(h)

# =====================================================
#  日次集計 & グラフ作成
# =====================================================

def aggregate_for_date(target_date: str) -> List[Tuple[str, str, str, int]]:
    """
    指定日付(YYYY-MM-DD)について
    player, slot, result ごとの回数を集計して返す。
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT player, slot, result, COUNT(*)
        FROM spins
        WHERE date = ?
        GROUP BY player, slot, result
        ORDER BY COUNT(*) DESC
        """,
        (target_date,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def aggregate_range(start: Optional[datetime], end: Optional[datetime]) -> List[Tuple[str, str, str, int]]:
    """
    任意の時間範囲 [start, end) について
    player, slot, result ごとの回数を集計して返す。
    ルーレット(スロット名に「ルーレット / Roulette」を含むもの)は除外。
    戻り値: [(player, slot, result, count), ...]
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if start is None or end is None:
        # 全期間（ルーレット除外）
        cur.execute(
            """
            SELECT player, slot, result, COUNT(*)
            FROM spins
            WHERE slot NOT LIKE '%ルーレット%'
              AND slot NOT LIKE '%Roulette%'
            GROUP BY player, slot, result
            ORDER BY COUNT(*) DESC
            """
        )
        rows = cur.fetchall()
    else:
        # 期間指定（ルーレット除外）
        cur.execute(
            """
            SELECT player, slot, result, COUNT(*)
            FROM spins
            WHERE ts >= ? AND ts < ?
              AND slot NOT LIKE '%ルーレット%'
              AND slot NOT LIKE '%Roulette%'
            GROUP BY player, slot, result
            ORDER BY COUNT(*) DESC
            """,
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )
        rows = cur.fetchall()

    conn.close()
    return rows


def aggregate_roulette_range(start: Optional[datetime], end: Optional[datetime]) -> List[Tuple[str, int]]:
    """
    指定した時間範囲 [start, end) について
    ルーレットの player ごとの獲得金額合計を集計して返す。
    戻り値: [(player, total_amount), ...]
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if start is None or end is None:
        cur.execute(
            """
            SELECT player, SUM(amount)
            FROM roulette_hits
            GROUP BY player
            ORDER BY SUM(amount) DESC
            """
        )
    else:
        cur.execute(
            """
            SELECT player, SUM(amount)
            FROM roulette_hits
            WHERE ts >= ? AND ts < ?
            GROUP BY player
            ORDER BY SUM(amount) DESC
            """,
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )

    rows = cur.fetchall()
    conn.close()
    return rows



def aggregate_money_range(start: Optional[datetime], end: Optional[datetime]) -> List[Tuple[str, str, int]]:
    """
    指定した時間範囲 [start, end) について
    player, slot ごとの獲得金額合計を集計して返す。
    ルーレット(スロット名に「ルーレット / Roulette」を含むもの)は除外。
    戻り値: [(player, slot, total_yen), ...]
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if start is None or end is None:
        cur.execute(
            """
            SELECT player, slot, result, raw_message
            FROM spins
            WHERE slot NOT LIKE '%ルーレット%'
              AND slot NOT LIKE '%Roulette%'
            """
        )
    else:
        cur.execute(
            """
            SELECT player, slot, result, raw_message
            FROM spins
            WHERE ts >= ? AND ts < ?
              AND slot NOT LIKE '%ルーレット%'
              AND slot NOT LIKE '%Roulette%'
            """,
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )

    rows = cur.fetchall()
    conn.close()

    sums: dict[tuple[str, str], int] = {}

    for player, slot, result, raw in rows:
        text = result or ""
        # result から「XXXX円」を探す。なければ raw_message からも探す
        m = re.search(r"([0-9,]+)円", text)
        if not m and raw:
            m = re.search(r"([0-9,]+)円", raw)

        if not m:
            continue  # 金額が含まれてないログはスキップ

        yen = int(m.group(1).replace(",", ""))

        key = (player, slot)
        sums[key] = sums.get(key, 0) + yen

    result_list: List[Tuple[str, str, int]] = [
        (player, slot, total_yen) for (player, slot), total_yen in sums.items()
    ]
    result_list.sort(key=lambda x: x[2], reverse=True)
    return result_list



def create_money_plot(label: str, rows: List[Tuple[str, str, int]]) -> Optional[str]:
    """
    獲得金額集計結果から棒グラフPNGを生成し、ファイルパスを返す。
    rows: [(player, slot, total_yen), ...]
    """
    if not rows:
        return None

    labels = []
    values = []
    for player, slot, total_yen in rows:
        labels.append(f"{player}\n{slot}")
        values.append(total_yen)

    plt.figure(figsize=(12, 6))
    plt.bar(range(len(values)), values)
    plt.xticks(range(len(values)), labels, rotation=90)
    plt.title(f"Man10Casino 獲得金額合計 ({label})")
    plt.ylabel("獲得金額(円)")
    plt.tight_layout()

    safe_label = label.replace(" ", "_").replace("〜", "_").replace(":", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(BASE_DIR, f"casino_money_{safe_label}_{ts}.png")

    plt.savefig(out_path)
    plt.close()
    return out_path

def create_roulette_plot(label: str, rows: List[Tuple[str, int]]) -> Optional[str]:
    """
    ルーレット獲得金額集計結果から棒グラフPNGを生成し、ファイルパスを返す。
    rows: [(player, total_amount), ...]
    """
    if not rows:
        return None

    players = [r[0] for r in rows]
    values = [r[1] for r in rows]

    plt.figure(figsize=(12, 6))
    plt.bar(range(len(values)), values)
    plt.xticks(range(len(values)), players, rotation=90)
    plt.title(f"Man10 Roulette 獲得金額合計 ({label})")
    plt.ylabel("獲得金額(円)")
    plt.tight_layout()

    safe_label = label.replace(" ", "_").replace("〜", "_").replace(":", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(BASE_DIR, f"roulette_{safe_label}_{ts}.png")

    plt.savefig(out_path)
    plt.close()
    return out_path



def create_range_plot(label: str, rows: List[Tuple[str, str, str, int]]) -> Optional[str]:
    """
    任意ラベルの集計結果から棒グラフPNGを生成し、ファイルパスを返す。
    """
    if not rows:
        return None

    labels = []
    values = []
    for player, slot, result, count in rows:
        labels.append(f"{player}\n{slot}\n{result}")
        values.append(count)

    plt.figure(figsize=(12, 6))
    plt.bar(range(len(values)), values)
    plt.xticks(range(len(values)), labels, rotation=90)
    plt.title(f"Man10Casino 当たり集計 ({label})")
    plt.ylabel("回数")

    # ★ Y軸を整数だけにする
    ax = plt.gca()
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))

    plt.tight_layout()

    # ファイル名はラベルと現在時刻から安全な文字だけ使う
    safe_label = label.replace(" ", "_").replace("〜", "_").replace(":", "")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(BASE_DIR, f"casino_{safe_label}_{ts}.png")

    plt.savefig(out_path)
    plt.close()
    return out_path

def create_daily_plot(target_date: str, rows: List[Tuple[str, str, str, int]]) -> Optional[str]:
    """
    集計結果から棒グラフPNGを生成し、ファイルパスを返す。
    データが空なら None。
    """
    if not rows:
        return None

    labels = []
    values = []
    for player, slot, result, count in rows:
        labels.append(f"{player}\n{slot}\n{result}")
        values.append(count)

    plt.figure(figsize=(12, 6))
    plt.bar(range(len(values)), values)
    plt.xticks(range(len(values)), labels, rotation=90)
    plt.title(f"Man10Casino 当たり集計 ({target_date})")
    plt.ylabel("回数")

    # ★ Y軸を整数だけにする
    ax = plt.gca()
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))

    plt.tight_layout()

    out_path = os.path.join(BASE_DIR, f"casino_{target_date}.png")
    plt.savefig(out_path)
    plt.close()
    return out_path


async def send_daily_report():
    """
    前日分の集計を行い、グラフとテキストを Discord に送る。
    """
    channel = bot.get_channel(DAILY_REPORT_CHANNEL_ID)
    if channel is None:
        print("DAILY_REPORT_CHANNEL_ID が不正です")
        return

    # 今が 2025-11-24 3:50 としたら、集計対象は 2025-11-23
    target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = aggregate_for_date(target_date)

    if not rows:
        await channel.send(f"📊 {target_date} のデータはありませんでした。")
        return

    # テキストまとめ（上位10件くらい）
    lines = []
    for player, slot, result, count in rows[:10]:
        lines.append(f"- **{player}** / {slot} / {result} → {count}回")

    text = f"📊 **{target_date} の Man10Casino 当たり集計**\n" + "\n".join(lines)

    # グラフ画像を生成
    img_path = create_daily_plot(target_date, rows)

    if img_path and os.path.exists(img_path):
        file = discord.File(img_path, filename=os.path.basename(img_path))
        await channel.send(content=text, file=file)
    else:
        await channel.send(text)


# 3:50 に合わせて毎日実行するループ
@tasks.loop(hours=24)
async def daily_report_loop():
    await send_daily_report()


@daily_report_loop.before_loop
async def before_daily_report_loop():
    """
    次の 3:50 までスリープしてからループをスタートさせる。
    """
    await bot.wait_until_ready()
    while True:
        now = datetime.now()
        target = now.replace(hour=3, minute=50, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        print(f"次の日次集計まで {int(wait_sec)} 秒待機 ({target})")
        await asyncio.sleep(wait_sec)
        break

# =====================================================
#  イベント & コマンド
# =====================================================

# ---------- VC監視 (/voice) ----------

@bot.tree.command(name="voice", description="VC入退室ログのオン/オフを切り替えます")
async def voice_command(interaction: discord.Interaction):
    global is_voice_logging
    is_voice_logging = not is_voice_logging
    state = "ON" if is_voice_logging else "OFF"
    await interaction.response.send_message(
        f"VC監視を **{state}** にしました。", ephemeral=True
    )


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if not is_voice_logging or VOICE_CHANNEL_ID is None:
        return

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        return

    before_ch = before.channel
    after_ch = after.channel

    if before_ch is None and after_ch is not None:
        msg = f"🔔 {member.display_name} が **{after_ch.name}** に入室しました"
    elif before_ch is not None and after_ch is None:
        msg = f"👋 {member.display_name} が **{before_ch.name}** から退出しました"
    elif before_ch is not None and after_ch is not None and before_ch.id != after_ch.id:
        msg = f"🔄 {member.display_name} が **{before_ch.name} → {after_ch.name}** に移動しました"
    else:
        return

    await channel.send(msg)


# ---------- Casinoログ監視本体 ----------

async def tail_casino_log():
    global is_casino_logging

    await bot.wait_until_ready()
    channel = bot.get_channel(CASINO_CHANNEL_ID)
    if channel is None:
        print("CASINO_CHANNEL_ID が不正です")
        return

    try:
        f = open(MC_LOG_PATH, "r", encoding="utf-8")
    except FileNotFoundError:
        await channel.send(f"⚠ latest.log が見つかりません: `{MC_LOG_PATH}`")
        is_casino_logging = False
        return

    f.seek(0, os.SEEK_END)
    await channel.send("🎰 Man10Casino ログ監視を開始しました。")

    try:
        while is_casino_logging and not bot.is_closed():
            line = f.readline()
            if not line:
                await asyncio.sleep(1.0)
                continue

            if "[CHAT]" not in line:
                continue

            try:
                msg = line.split("[CHAT]", 1)[1].strip()
            except Exception:
                continue

            if not any(k in msg for k in CASINO_KEYWORDS):
                continue
            # 重複メッセージチェック
            global last_casino_message
            if msg == last_casino_message:
                continue  # 前回と同じ内容なら送らない＆保存しない
            last_casino_message = msg

            # Discordにそのまま流す
            await channel.send(f"🎰 {msg}")

            # 解析してDB保存
            parsed = parse_casino_message(msg)
            if parsed is not None:
                player, slot, result = parsed
                save_spin(player, slot, result, msg)
            # ★ スロットごとの「当たり」を判定して slot_hits に保存
            detect_and_save_slot_hits(msg, parsed)
            
            roulette = parse_roulette_message(msg)
            if roulette is not None:
                r_player, r_amount = roulette
                save_roulette_win(r_player, r_amount, msg)
    finally:
        f.close()
        await channel.send("🛑 Man10Casino ログ監視を停止しました。")


# ---------- /casino コマンド ----------

@bot.tree.command(name="casino", description="Man10Casinoログ監視のオン/オフを切り替えます")
async def casino_command(interaction: discord.Interaction):
    global is_casino_logging, casino_task

    if not is_casino_logging:
        is_casino_logging = True
        casino_task = bot.loop.create_task(tail_casino_log())
        await interaction.response.send_message(
            "🎰 カジノログ監視を **ON** にしました。", ephemeral=True
        )
    else:
        is_casino_logging = False
        if casino_task and not casino_task.done():
            casino_task.cancel()
        await interaction.response.send_message(
            "🛑 カジノログ監視を **OFF** にしました。", ephemeral=True
        )

async def handle_chat(request):
    """
    PC側から POST されるチャットを受け取り、
    Casino 解析 → DB保存 → Discordに送信
    の流れを実行する。
    """
    try:
        data = await request.json()
        msg = data.get("msg", "").strip()

        if not msg:
            return web.Response(text="no msg")

        # Discord側へ送信
        global last_casino_message
        if msg != last_casino_message:
            last_casino_message = msg

            # Discord側へ送信
            channel = bot.get_channel(CASINO_CHANNEL_ID)
            if channel:
                await channel.send(f"🎰 {msg}")

            # casino解析＆保存
            parsed = parse_casino_message(msg)
            if parsed is not None:
                player, slot, result = parsed
                save_spin(player, slot, result, msg)

            # スロット当たり
            detect_and_save_slot_hits(msg, parsed)

            # ルーレット獲得
            roulette = parse_roulette_message(msg)
            if roulette is not None:
                r_player, r_amount = roulette
                save_roulette_win(r_player, r_amount, msg)

        return web.Response(text="ok")

    except Exception as e:
        print("handle_chat error:", e)
        return web.Response(text="error", status=500)


async def start_web_server():
    """
    aiohttp の Web サーバー起動
    """
    app = web.Application()
    app.router.add_post('/chat', handle_chat)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', 5005)
    await site.start()
    print("チャット受信用API: ポート5005で起動中")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # スラッシュコマンド同期
    try:
        await bot.tree.sync()
    except Exception as e:
        print("Sync error:", e)

    # aiohttp API 起動
    asyncio.create_task(start_web_server())

    # 日次集計ループ開始
    if not daily_report_loop.is_running():
        daily_report_loop.start()
        
    
@bot.tree.command(
    name="picture",
    description="カジノ/ルーレットの集計グラフを送ります。all/today/week/month"
)
async def picture_command(
    interaction: discord.Interaction,
    period: str = "today",
    game: str = "casino"
):
    """
    /picture [period] [game]

    period:
      - all   : 全期間
      - today : 今日 (4:00〜今)
      - week  : 直近7日 (4:00区切り)
      - month : 直近30日 (4:00区切り)

    game:
      - casino   : スロット系 (デフォルト)
      - roulette : ルーレット獲得
    """
    await interaction.response.defer(thinking=True)

    period = period.lower()
    game = game.lower()
    now = datetime.now()

    # 4:00 区切りの「今日」の開始時刻を求める
    stat_base_date = now.date()
    if now.hour < 4:
        stat_base_date = stat_base_date - timedelta(days=1)
    start_today = datetime.combine(stat_base_date, datetime.min.time()) + timedelta(hours=4)

    if period in ("today", "day", "t", "d"):
        start = start_today
        end = now
        label = f"today {start.strftime('%Y-%m-%d')}"
    elif period in ("week", "w"):
        start = start_today - timedelta(days=6)  # 今日含め直近7日
        end = now
        label = f"week {start.strftime('%Y-%m-%d')}〜{stat_base_date.strftime('%Y-%m-%d')}"
    elif period in ("month", "m"):
        start = start_today - timedelta(days=29)  # 今日含め直近30日
        end = now
        label = f"month {start.strftime('%Y-%m-%d')}〜{stat_base_date.strftime('%Y-%m-%d')}"
    else:
        start = None
        end = None
        label = "all"

    # =============================
    #  ルーレットモード
    # =============================
    if game in ("roulette", "r"):
        rows_roulette = aggregate_roulette_range(start, end)

        if not rows_roulette:
            await interaction.followup.send(f"🎯 Roulette | {label} のデータはありませんでした。")
            return

        # テキストまとめ（上位10人）
        lines: List[str] = []
        lines.append("**▼ ルーレット獲得金額ランキング (上位10件)**")
        for player, total_amount in rows_roulette[:10]:
            lines.append(f"- {player} → {total_amount} 円")

        text = f"🎯 **Roulette | {label} の集計**\n" + "\n".join(lines)

        # グラフ画像生成
        img_path = create_roulette_plot(label, rows_roulette)
        files = []
        if img_path and os.path.exists(img_path):
            files.append(discord.File(img_path, filename=os.path.basename(img_path)))

        if files:
            await interaction.followup.send(content=text, files=files)
        else:
            await interaction.followup.send(text)

        return

    # =============================
    #  通常（スロット系）モード
    # =============================
    rows_count = aggregate_range(start, end)
    rows_money = aggregate_money_range(start, end)

    if not rows_count and not rows_money:
        await interaction.followup.send(f"📊 {label} のデータはありませんでした。")
        return

    lines: List[str] = []
    if rows_count:
        lines.append("**▼ 当たり回数ランキング (上位10件)**")
        for player, slot, result, count in rows_count[:10]:
            lines.append(f"- {player} / {slot} / {result} → {count}回")
        lines.append("")

    if rows_money:
        lines.append("**▼ 獲得金額ランキング (上位10件)**")
        for player, slot, total_yen in rows_money[:10]:
            lines.append(f"- {player} / {slot} → {total_yen} 円")

    text = f"📊 **{label} の Man10Casino 集計**\n" + "\n".join(lines)

    img_count = create_range_plot(label, rows_count) if rows_count else None
    img_money = create_money_plot(label, rows_money) if rows_money else None

    files = []
    if img_count and os.path.exists(img_count):
        files.append(discord.File(img_count, filename=os.path.basename(img_count)))
    if img_money and os.path.exists(img_money):
        files.append(discord.File(img_money, filename=os.path.basename(img_money)))

    if files:
        await interaction.followup.send(content=text, files=files)
    else:
        await interaction.followup.send(text)



# =====================================================
#  実行
# =====================================================

if __name__ == "__main__":
    bot.run(TOKEN)
