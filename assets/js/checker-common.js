(function () {
  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeNumber(value) {
    if (value === '' || value === null || value === undefined) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function readFormValues(form) {
    const data = {};
    new FormData(form).forEach((value, key) => {
      data[key] = normalizeNumber(value);
    });
    return data;
  }

  function renderResult(target, result) {
    target.innerHTML = `
      <div class="checker-result checker-result-${escapeHtml(result.rank || 'info')}">
        <div class="checker-result-rank">${escapeHtml(result.badge || '判定')}</div>
        <div class="checker-result-title">${escapeHtml(result.title || '')}</div>
        <p class="checker-result-message">${escapeHtml(result.message || '')}</p>
      </div>
    `;
  }

  function defaultJudge(values, config) {
    const game = values.game;
    if (game === null) {
      return {
        rank: 'info',
        badge: '入力待ち',
        title: '数値を入力してください',
        message: '最低でもゲーム数の入力が必要です。'
      };
    }

    const excellent = config.thresholds?.excellent ?? null;
    const good = config.thresholds?.good ?? null;
    const caution = config.thresholds?.caution ?? null;

    if (excellent !== null && game >= excellent) {
      return {
        rank: 'excellent',
        badge: '◎',
        title: `${excellent}G以上なので強めの狙い目`,
        message: '期待値の取りやすいラインとして扱うテンプレート判定です。'
      };
    }

    if (good !== null && game >= good) {
      return {
        rank: 'good',
        badge: '○',
        title: `${good}G以上なので狙い目候補`,
        message: 'ホール状況や持ちメダルも加味して判断してください。'
      };
    }

    if (caution !== null && game >= caution) {
      return {
        rank: 'caution',
        badge: '△',
        title: `${caution}G以上なので様子見ライン`,
        message: '条件が弱めなので、他の台状況も確認推奨です。'
      };
    }

    return {
      rank: 'ng',
      badge: '×',
      title: '現状は見送り寄り',
      message: 'テンプレート基準ではまだ浅めです。'
    };
  }

  window.SlotChecker = {
    create: function createChecker(options) {
      const form = document.querySelector(options.formSelector);
      const output = document.querySelector(options.outputSelector);
      const config = options.config || {};
      const judge = options.judge || function(values) {
        return defaultJudge(values, config);
      };

      if (!form || !output) {
        console.warn('SlotChecker: form or output not found.');
        return;
      }

      const update = function () {
        const values = readFormValues(form);
        const result = judge(values, config);
        renderResult(output, result);
      };

      form.addEventListener('input', update);
      form.addEventListener('change', update);
      update();
    }
  };
})();
