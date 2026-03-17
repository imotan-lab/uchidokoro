async function initBakiChecker() {
  const response = await fetch('assets/data/checkers/baki.json');
  const config = await response.json();

  SlotChecker.create({
    formSelector: '#checkerForm',
    outputSelector: '#checkerOutput',
    config,
    judge(values, cfg) {
      const game = values.game;
      const through = values.through ?? 0;
      const exchange = values.exchange ?? 0;
      const hint = values.hint ?? 0;

      if (game === null) {
        return {
          rank: 'info',
          badge: '入力待ち',
          title: 'ゲーム数を入力してください',
          message: '現在のCZ間ゲーム数を入れると、バキの狙い目をすぐ判定できます。',
          note: 'CZスルー回数も一緒に入れると、より実戦向きに見られます。'
        };
      }

      if (through >= 3) {
        return {
          rank: 'excellent',
          badge: '◎',
          special: '3スルー確定',
          title: '次回CZでAT確定のため即着席候補',
          message: cfg.notes.threeThrough,
          note: 'ゲーム数が浅くても打てる強い条件です。',
          facts: [
            { label: 'CZスルー回数', value: '3スルー' },
            { label: '現在のCZ間G数', value: `${game}G` },
            { label: '次回CZ', value: 'AT確定' },
            { label: '推奨', value: '0Gから着席候補' }
          ]
        };
      }

      if (through === 2) {
        const line = cfg.throughLines.twoThrough;
        const reached = game >= line;
        return {
          rank: reached ? 'excellent' : 'good',
          badge: reached ? '◎' : '○',
          title: reached ? '2スルーかつ狙い目ライン到達' : `2スルー台。目安まであと${line - game}G`,
          message: reached
            ? '2スルー台は次のCZ失敗でAT確定の3スルーへ近く、かなり打ちやすい状態です。'
            : '2スルー台は通常より優先度高めです。200G台後半に近づくほど狙いやすくなります。',
          note: cfg.notes.endorphin,
          facts: [
            { label: 'CZスルー回数', value: '2スルー' },
            { label: '現在のCZ間G数', value: `${game}G` },
            { label: '基準ライン', value: `${line}G〜` },
            { label: '交換率', value: exchange === 0 ? '等価' : '5.6枚交換' }
          ]
        };
      }

      const lineSet = exchange === 0 ? cfg.exchangeLines.equal : cfg.exchangeLines.fiveSix;
      let normalLine = lineSet.normal;
      let cautionLine = lineSet.caution;

      if (hint === 1) {
        normalLine = Math.max(80, normalLine - 80);
        cautionLine = Math.max(80, cautionLine - 80);
      } else if (hint === 2) {
        normalLine = 80;
        cautionLine = 80;
      }

      if (game >= normalLine) {
        return {
          rank: 'excellent',
          badge: '◎',
          title: `${normalLine}G以上なので狙い目ライン到達`,
          message: 'CZ間600G天井を踏まえた、現実的な狙い目ラインを超えています。',
          note: hint > 0 ? '示唆ありとして浅め補正をかけています。' : cfg.notes.endorphin,
          facts: [
            { label: 'CZスルー回数', value: `${through}スルー` },
            { label: '現在のCZ間G数', value: `${game}G` },
            { label: '通常ライン', value: `${normalLine}G〜` },
            { label: '天井', value: `${cfg.ceiling}G` }
          ]
        };
      }

      if (game >= cautionLine) {
        return {
          rank: 'caution',
          badge: '△',
          title: `候補ライン帯。目安まであと${Math.max(normalLine - game, 0)}G`,
          message: 'ほかに強い台がなければ検討余地ありです。エンドルフィンやボイス示唆の有無も見てください。',
          note: '天国示唆がある場合は132Gまでのフォローも候補になります。',
          facts: [
            { label: 'CZスルー回数', value: `${through}スルー` },
            { label: '現在のCZ間G数', value: `${game}G` },
            { label: '候補ライン', value: `${cautionLine}G〜` },
            { label: '本命ライン', value: `${normalLine}G〜` }
          ]
        };
      }

      return {
        rank: 'ng',
        badge: '×',
        title: `現状は見送り寄り。目安まであと${Math.max(normalLine - game, 0)}G`,
        message: '通常条件ではまだ浅めです。スルー回数の多い台や示唆あり台を優先した方が打ちやすいです。',
        note: 'PUSHボイス・ステージ・バキオーラの有無は必ず確認してください。',
        facts: [
          { label: 'CZスルー回数', value: `${through}スルー` },
          { label: '現在のCZ間G数', value: `${game}G` },
          { label: '本命ライン', value: `${normalLine}G〜` },
          { label: '交換率', value: exchange === 0 ? '等価' : '5.6枚交換' }
        ]
      };
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initBakiChecker().catch((error) => {
    const output = document.querySelector('#checkerOutput');
    if (output) {
      output.innerHTML = '<div class="checker-result checker-result-ng"><div class="checker-result-rank">!</div><div class="checker-result-title">チェッカーを読み込めませんでした</div><p class="checker-result-message">assets/data/checkers/baki.json の配置をご確認ください。</p></div>';
    }
    console.error(error);
  });
});
