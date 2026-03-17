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
      ? m.seo.title + " | 狙い目手帖"
      : m.name + " 狙い目・天井・やめどき | 狙い目手帖";

    let meta = document.querySelector('meta[name="description"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "description";
      document.head.appendChild(meta);
    }

    const strategy = m.strategy || "狙い目情報";
    meta.content = `${m.name}の天井・狙い目・やめどき・判定ツールを掲載。現在の狙い目は ${strategy}。`;
  } catch (e) {
    console.log(e);
  }
});