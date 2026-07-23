(function(){
  // slug を URLクエリ（?slug=xxx）または URLパス（/machines/{slug}/）から取得
  const params = new URLSearchParams(location.search);
  let slug = params.get("slug");
  if(!slug){
    const m = location.pathname.match(/\/machines\/([^\/]+)\/?(?:index\.html)?$/);
    if(m) slug = m[1];
  }
  if(!slug) return;

  fetch("/assets/data/machines.json")
    .then(res => res.json())
    .then(list => {
      const m = list.find(x => x.slug === slug);
      if(!m) return;

      const isPreview = m.status === "preview";
      const strategy = m.strategy || '';
      const info = m.info || '';
      const releaseDate = m.release_date || ''; // YYYY-MM-DD

      // 導入日を日本語フォーマットに（例: "2026-05-11" → "5月11日"）
      function formatJpDate(dateStr){
        if(!dateStr) return '';
        const match = String(dateStr).match(/^(\d{4})-(\d{2})-(\d{2})$/);
        if(!match) return '';
        return `${Number(match[2])}月${Number(match[3])}日`;
      }
      const releaseJp = formatJpDate(releaseDate);

      // タイトル生成（status別に切り替え）
      let title;
      if(isPreview){
        title = releaseJp
          ? `【先行】${m.name} ${releaseJp}導入｜天井・狙い目予想・解析判明次第更新`
          : `【先行】${m.name} 天井・狙い目予想｜解析判明次第更新`;
      } else {
        // このtitle生成が効くのはトランポリン（machine.html?slug=・noindex）のみ。
        // プリレンダ済み正規ページは alreadyBaked で上書きしないため、機種ごとの
        // ポチポチくん対応可否はプリレンダ側(build_machine_pages.py)が正しく焼く。
        // ここでは対応可否を判定できないため、汎用文言で誤称を避ける。
        title = `${m.name} 天井・狙い目・やめどき｜期待値・立ち回りガイド`;
      }

      // description生成
      let desc;
      if(isPreview){
        desc = releaseJp
          ? `${releaseJp}導入予定の${m.name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。`
          : `${m.name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。`;
      } else {
        desc = strategy
          ? `${m.name}の天井・狙い目・やめどき・設定差を徹底解説。${strategy}。期待値重視の立ち回りをサポートします。`
          : `${m.name}の天井・狙い目・やめどき・設定差を徹底解説。${info}の立ち回りを期待値重視でサポートします。`;
      }

      // プリレンダ済みページ（/machines/{slug}/）は build_machine_pages.py が
      // 正しい title/description/OGP/canonical を焼き込み済み。実行時に上書きすると
      // 非対応機種で「ポチポチくん対応」表記が復活する等の不整合が起きるため、
      // ベイク済み（title がテンプレ既定でない）なら上書きしない。
      // トランポリンの machine.html?slug= 側（title がテンプレ既定）のみ動的生成する。
      const alreadyBaked = !document.title.includes('機種ページ');

      function setMeta(selector, attr, name, content){
        let el = document.querySelector(selector);
        if(!el){
          el = document.createElement("meta");
          el.setAttribute(attr, name);
          document.head.appendChild(el);
        }
        el.content = content;
      }

      if (!alreadyBaked) {
        document.title = title;
        setMeta('meta[name="description"]', 'name', 'description', desc);

        // OGP
        setMeta('meta[property="og:title"]', 'property', 'og:title', title);
        setMeta('meta[property="og:description"]', 'property', 'og:description', desc);
        setMeta('meta[property="og:type"]', 'property', 'og:type', 'article');
        setMeta('meta[property="og:url"]', 'property', 'og:url', `https://uchidokoro.com/machines/${slug}/`);
        setMeta('meta[property="og:site_name"]', 'property', 'og:site_name', 'うちどころ。');
        setMeta('meta[property="og:image"]', 'property', 'og:image', 'https://uchidokoro.com/assets/img/ogp.png');

        // Twitter Card
        setMeta('meta[name="twitter:card"]', 'name', 'twitter:card', 'summary_large_image');
        setMeta('meta[name="twitter:site"]', 'name', 'twitter:site', '@uchidokoro');
        setMeta('meta[name="twitter:title"]', 'name', 'twitter:title', title);
        setMeta('meta[name="twitter:description"]', 'name', 'twitter:description', desc);
        setMeta('meta[name="twitter:image"]', 'name', 'twitter:image', 'https://uchidokoro.com/assets/img/ogp.png');

        // canonical link
        let canonical = document.querySelector('link[rel="canonical"]');
        if(!canonical){
          canonical = document.createElement("link");
          canonical.rel = "canonical";
          document.head.appendChild(canonical);
        }
        canonical.href = `https://uchidokoro.com/machines/${slug}/`;
      }

      // JSON-LD 構造化データ
      // 日付は出力しない（release_date=導入日は記事の公開日ではないため。build_machine_pages.pyと同一方針）
      const jsonLd = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "image": "https://uchidokoro.com/assets/img/ogp.png",
        "author": {
          "@type": "Organization",
          "name": "うちどころ。",
          "url": "https://uchidokoro.com"
        },
        "publisher": {
          "@type": "Organization",
          "name": "うちどころ。",
          "url": "https://uchidokoro.com",
          "logo": {
            "@type": "ImageObject",
            "url": "https://uchidokoro.com/assets/img/ogp.png"
          }
        },
        "mainEntityOfPage": {
          "@type": "WebPage",
          "@id": `https://uchidokoro.com/machines/${slug}/`
        }
      };
      // プリレンダ済みページには build_machine_pages.py がJSON-LDを静的に焼き込み済み。
      // 二重出力を避けるため、既にある場合は追加しない（クエリURL等の旧シェル表示時のみ動的追加）
      if (!document.querySelector('script[type="application/ld+json"]')) {
        const ldScript = document.createElement("script");
        ldScript.type = "application/ld+json";
        ldScript.textContent = JSON.stringify(jsonLd);
        document.head.appendChild(ldScript);
      }
    });
})();
