document.addEventListener("DOMContentLoaded", async () => {
  try {
    const params = new URLSearchParams(location.search);
    const slug = params.get("slug");
    if (!slug) return;

    const res = await fetch("assets/data/machines.json");
    const machines = await res.json();
    const m = machines.find(x => x.slug === slug);
    if (!m) return;

    document.title = (m.seo && m.seo.title)
      ? m.seo.title + " | うちどころ。"
      : m.name + " 狙い目・天井・やめどき | うちどころ。";

    let meta = document.querySelector('meta[name="description"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "description";
      document.head.appendChild(meta);
    }

    const strategy = m.strategy || "詳細準備中";
    meta.content = `${m.name}の狙い目・天井・やめどきをまとめた攻略ページ。G数を入力するだけで打っていいか即判定できます。現在の狙い目の目安：${strategy}。`;
  } catch (e) {
    console.log(e);
  }
});
