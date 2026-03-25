(function(){
  const params = new URLSearchParams(location.search);
  const slug = params.get("slug");
  if(!slug) return;

  fetch("assets/data/machines.json")
    .then(res => res.json())
    .then(list => {
      const m = list.find(x => x.slug === slug);
      if(!m) return;

      const title = `${m.name} 狙い目・天井・期待値まとめ`;
      const strategy = m.strategy || '';
      const info = m.info || '';
      const desc = strategy
        ? `${m.name}の狙い目・天井・やめどき・設定判別まとめ。${strategy}。${info}の立ち回り情報を掲載。`
        : `${m.name}の狙い目・天井・やめどき・設定判別を分かりやすく解説。期待値重視で立ち回るための情報を掲載しています。`;

      document.title = title;

      let metaDesc = document.querySelector('meta[name="description"]');
      if(!metaDesc){
        metaDesc = document.createElement("meta");
        metaDesc.name = "description";
        document.head.appendChild(metaDesc);
      }
      metaDesc.content = desc;

      // OG
      let ogTitle = document.querySelector('meta[property="og:title"]');
      if(!ogTitle){
        ogTitle = document.createElement("meta");
        ogTitle.setAttribute("property","og:title");
        document.head.appendChild(ogTitle);
      }
      ogTitle.content = title;

      let ogDesc = document.querySelector('meta[property="og:description"]');
      if(!ogDesc){
        ogDesc = document.createElement("meta");
        ogDesc.setAttribute("property","og:description");
        document.head.appendChild(ogDesc);
      }
      ogDesc.content = desc;

      // JSON-LD 構造化データ
      const jsonLd = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "author": { "@type": "Organization", "name": "うちどころ。" },
        "publisher": {
          "@type": "Organization",
          "name": "うちどころ。",
          "url": "https://uchidokoro.com"
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