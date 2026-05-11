"""
2回目の文体修正：残り49箇所の常体文を丁寧体に書き換え。

文字列マッチで全角括弧をそのまま使う。半角への変換は試みない。
"""
import json, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
DETAIL_DIR = BASE / "assets" / "data" / "machine-details"

# (slug, 元文字列(部分一致でOK), 置換後文字列) のリスト
# 部分一致で置換するので、より長い特徴的なフレーズを使う
REPLACEMENTS = [
    # enen2
    ("enen2", "炎炎ループ確率を主軸にする。", "炎炎ループ確率を主軸にしましょう。"),
    # okidoki_duo_encore
    ("okidoki_duo_encore", "スルー回数が多いほど狙い目が浅くなる。", "スルー回数が多いほど狙い目が浅くなります。"),
    # mhrise
    ("mhrise", "は浅いラインから候補になる。", "は浅いラインから候補になります。"),
    ("mhrise", "ポイント残量の確認が重要な判断材料になる。", "ポイント残量の確認が重要な判断材料になります。"),
    ("mhrise", "天井狙いから設定狙いに切り替えて続行を検討する。", "天井狙いから設定狙いに切り替えて続行を検討しましょう。"),
    ("mhrise", "着席前に必ずカムラポイントとスルー回数を確認する。", "着席前に必ずカムラポイントとスルー回数を確認しましょう。"),
    # hokuto
    ("hokuto", "朝一はリセット確定根拠がある場合のみ100G前後から候補にする。", "朝一はリセット確定根拠がある場合のみ100G前後から候補にしましょう。"),
    ("hokuto", "取りこぼすと期待値に直接影響する。", "取りこぼすと期待値に直接影響します。"),
    # sengoku_otome4
    ("sengoku_otome4", "ため、290〜300G台の台も候補になる。", "ため、290〜300G台の台も候補になります。"),
    # chibaryo2
    ("chibaryo2", "ポチポチくんを活用すると設定推測の補助になる。", "ポチポチくんを活用すると設定推測の補助になります。"),
    ("chibaryo2", "期待値を正確に出す前提になる。", "期待値を正確に出す前提になります。"),
    # tolove_darkness
    ("tolove_darkness", "リセット狙いなら70G〜から期待値プラスになる。", "リセット狙いなら70G〜から期待値プラスになります。"),
    # yoshimune
    ("yoshimune", "深い台ほど費用対効果が高くなる。", "深い台ほど費用対効果が高くなります。"),
    ("yoshimune", "この恩恵を逃さないことが収支向上の鍵になる。", "この恩恵を逃さないことが収支向上の鍵になります。"),
    ("yoshimune", "天国の1回でも取りこぼすと大きなロスになる。", "天国の1回でも取りこぼすと大きなロスになります。"),
    # goblin
    ("goblin", "藤丸コインを必ず確認する。", "藤丸コインを必ず確認しましょう。"),
    ("goblin", "終日の目安になるため継続を検討する。", "終日の目安になるため継続を検討しましょう。"),
    # funky_juggler2
    ("funky_juggler2", "BIGが突出して引けているかどうかを最重視する。", "BIGが突出して引けているかどうかを最重視します。"),
    ("funky_juggler2", "Aタイプなのでゲーム数によるヤメ時の概念はない。", "Aタイプなのでゲーム数によるヤメ時の概念はありません。"),
    ("funky_juggler2", "REGばかり引いてBIGが少ない台は設定6の可能性が低くなる。", "REGばかり引いてBIGが少ない台は設定6の可能性が低くなります。"),
    ("funky_juggler2", "判断したら任意のタイミングでヤメてよい。", "判断したら任意のタイミングでヤメてOKです。"),
    ("funky_juggler2", "長時間サンプルで判断する。", "長時間サンプルで判断しましょう。"),
    # onepunchman
    ("onepunchman", "65〜128Gのゾーンを意識する。", "65〜128Gのゾーンを意識しましょう。"),
    # dumbbell
    ("dumbbell", "設定判別に切り替えることも選択肢になる。", "設定判別に切り替えることも選択肢になります。"),
    ("dumbbell", "立ち回りの精度を大きく左右する。", "立ち回りの精度を大きく左右します。"),
    ("dumbbell", "0回340G→1回200G→2回150G→3回100G→4〜5回50Gと急激に浅くなる。", "0回340G→1回200G→2回150G→3回100G→4〜5回50Gと急激に浅くなります。"),
    # happy_juggler_v3
    ("happy_juggler_v3", "ゲーム数による期待値計算は存在しない。", "ゲーム数による期待値計算は存在しません。"),
    ("happy_juggler_v3", "REG確率とぶどう確率の推移を見て判断する。", "REG確率とぶどう確率の推移を見て判断しましょう。"),
    # tensura
    ("tensura", "保守的に動いた方が安定する。", "保守的に動いた方が安定します。"),
    ("tensura", "継続G数も設定示唆の補助材料になる。", "継続G数も設定示唆の補助材料になります。"),
    ("tensura", "復活示唆のため絶対に即ヤメしない。", "復活示唆のため絶対に即ヤメしないでください。"),
    # super_blackjack
    ("super_blackjack", "666G+αに短縮され狙い目が浅くなる。", "666G+αに短縮され狙い目が浅くなります。"),
    ("super_blackjack", "設定狙いに切り替える選択肢も検討する。", "設定狙いに切り替える選択肢も検討しましょう。"),
    ("super_blackjack", "BIG間ゲーム数を正確に把握してから着席する。", "BIG間ゲーム数を正確に把握してから着席しましょう。"),
    # kizumonogatari
    ("kizumonogatari", "メニュー画面右下で鬼血闘回数を確認する。", "メニュー画面右下で鬼血闘回数を確認しましょう。"),
    ("kizumonogatari", "設定狙いに変更を検討する。", "設定狙いに変更を検討しましょう。"),
    # sf5
    ("sf5", "ノーマルタイプ（天井なし）のため、ゲーム数による期待値計算は存在しない。", "ノーマルタイプ（天井なし）のため、ゲーム数による期待値計算は存在しません。"),
    # baki
    ("baki", "PUSHボイスで次のモードを確認する。", "PUSHボイスで次のモードを確認しましょう。"),
    # sao
    ("sao", "ボーナス間天井999Gも搭載し、500G〜から期待値が発生する。", "ボーナス間天井999Gも搭載し、500G〜から期待値が発生します。"),
    ("sao", "スルーカウントが引き継がれているかを最優先で確認する。", "スルーカウントが引き継がれているかを最優先で確認しましょう。"),
    # madomagi_forte
    ("madomagi_forte", "プチボーナス当選では天井（BIG間）がリセットされない。", "プチボーナス当選では天井（BIG間）がリセットされません。"),
    # burning_express（基本スペック「設定段階」は表記。常体扱いは検出ミス。あえて触らない）
    # nanatsuma（基本スペック「機種名」も表記項目。常体扱いは検出ミス。あえて触らない）
    # shaman_king
    ("shaman_king", "AT・ボーナス中はカウントされない。", "AT・ボーナス中はカウントされません。"),
    ("shaman_king", "スルー回数が増えるほど低いゲーム数から狙い目になる。", "スルー回数が増えるほど低いゲーム数から狙い目になります。"),
    # tenken
    ("tenken", "ボーナスが当選してもAT間カウントはリセットされない。", "ボーナスが当選してもAT間カウントはリセットされません。"),
    ("tenken", "リセット台は浅いゲーム数から狙い目になる。", "リセット台は浅いゲーム数から狙い目になります。"),
    ("tenken", "1000G超えていれば追加で狙い目になる。", "1000G超えていれば追加で狙い目になります。"),
    # valvrave
    ("valvrave", "ヤメ時は状況によって異なる。", "ヤメ時は状況によって異なります。"),
    ("valvrave", "回数確認が正確な立ち回りの前提となる。", "回数確認が正確な立ち回りの前提となります。"),
    # lupin_daikokaisha
    ("lupin_daikokaisha", "離席前に終了画面を必ず確認する。", "離席前に終了画面を必ず確認しましょう。"),
    # zettai_shougeki4
    ("zettai_shougeki4", "次の周期まで継続してから判断する。", "次の周期まで継続してから判断しましょう。"),
]


def main():
    by_slug = {}
    for slug, old, new in REPLACEMENTS:
        by_slug.setdefault(slug, []).append((old, new))

    total = 0
    for slug, pairs in by_slug.items():
        p = DETAIL_DIR / f"{slug}.json"
        if not p.is_file():
            print(f"[!] {slug}.json なし")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        changed = 0
        for s in d.get("sections", []):
            body = s.get("body")
            if not isinstance(body, list):
                continue
            for i, item in enumerate(body):
                if not isinstance(item, str):
                    continue
                for old, new in pairs:
                    if old in item:
                        body[i] = item.replace(old, new)
                        changed += 1
        if changed:
            p.write_text(
                json.dumps(d, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"✅ {slug}: {changed}箇所")
            total += 1
        else:
            print(f"❌ {slug}: マッチせず")

    print(f"\n合計 {total} 機種を書き換え")


if __name__ == "__main__":
    main()
