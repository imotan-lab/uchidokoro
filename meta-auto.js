(function(){
  const params = new URLSearchParams(location.search);
  const slug = params.get("slug");
  if(!slug) return;

  fetch("assets/data/machines.json")
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
        title = `${m.name} 天井・狙い目・やめどき｜小役カウンター ポチポチくん対応`;
      }

      // description生成
      let desc;
      if(isPreview){
        desc = releaseJp
          ? `${releaseJp}導入予定の${m.name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。`
          : `${m.name}の機種概要を先行公開。天井・狙い目・設定差などの解析データが判明次第、随時更新します。導入前から最新情報をチェック。`;
      } else {
        desc = strategy
          ? `${m.name}の天井・狙い目・やめどき・設定差を徹底解説。${strategy}。小役カウンター ポチポチくんで設定判別も可能。期待値重視の立ち回りガイド。`
          : `${m.name}の天井・狙い目・やめどき・設定差を徹底解説。小役カウンター ポチポチくんで設定判別も可能。${info}の立ち回りを期待値重視でサポート。`;
      }

      // タイトル・description反映
      document.title = title;

      function setMeta(selector, attr, name, content){
        let el = document.querySelector(selector);
        if(!el){
          el = document.createElement("meta");
          el.setAttribute(attr, name);
          document.head.appendChild(el);
        }
        el.content = content;
      }

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

      // 日付情報（JSON-LDに使う）
      const todayIso = new Date().toISOString().split('T')[0];
      const datePublished = releaseDate || todayIso;

      // JSON-LD 構造化データ（強化版）
      const jsonLd = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "image": "https://uchidokoro.com/assets/img/ogp.png",
        "datePublished": datePublished,
        "dateModified": todayIso,
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
      const ldScript = document.createElement("script");
      ldScript.type = "application/ld+json";
      ldScript.textContent = JSON.stringify(jsonLd);
      document.head.appendChild(ldScript);
    });
})();
