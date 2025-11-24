import os
import time
import requests

VPS_HOST = "160.251.178.208"  # 例: "160.251.xxx.xxx"
VPS_PORT = 5005
VPS_URL = f"http://{VPS_HOST}:{VPS_PORT}/chat"

LOG_PATH = r"C:\Users\Maeda\AppData\Roaming\.minecraft\logs\latest.log"

def main():
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        print("latest.log 監視開始")

        while True:
            line = f.readline()
            if not line:
                time.sleep(1.0)
                continue

            if "[CHAT]" not in line:
                continue

            msg = line.split("[CHAT]", 1)[1].strip()

            try:
                requests.post(VPS_URL, json={"msg": msg}, timeout=3)
            except Exception as e:
                print("送信エラー:", e)

if __name__ == "__main__":
    main()
