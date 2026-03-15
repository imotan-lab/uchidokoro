async function loadMachines() {
  const list = document.getElementById("machineList");
  const count = document.getElementById("machineCount");
  const search = document.getElementById("machineSearch");
  const empty = document.getElementById("machineEmpty");

  try {
    const res = await fetch("assets/data/machines.json");
    if (!res.ok) throw new Error("machines.json の読み込みに失敗しました。");
    const machines = await res.json();

    const render = (keyword = "") => {
      const q = keyword.trim().toLowerCase();
      const filtered = machines.filter((m) => {
        return (
          m.name.toLowerCase().includes(q) ||
          m.slug.toLowerCase().includes(q)
        );
      });

      count.textContent = `${filtered.length}機種`;
      list.innerHTML = "";

      if (!filtered.length) {
        empty.hidden = false;
        return;
      }

      empty.hidden = true;

      filtered.forEach((m) => {
        const card = document.createElement("article");
        card.className = "machine-card";

        card.innerHTML = `
          <div class="machine-card-head">
            <span class="machine-badge">MACHINE</span>
            <h2 class="machine-title">${m.name}</h2>
          </div>
          <p class="machine-slug">${m.slug}</p>
          <div class="machine-links">
            <a class="btn" href="${m.article}">記事を見る</a>
            <a class="btn btn-sub" href="${m.checker}">チェッカー</a>
          </div>
        `;

        list.appendChild(card);
      });
    };

    search.addEventListener("input", (e) => render(e.target.value));
    render();
  } catch (err) {
    list.innerHTML = "";
    empty.hidden = false;
    empty.textContent = "機種一覧の読み込みに失敗しました。assets/data/machines.json を確認してください。";
    console.error(err);
  }
}

document.addEventListener("DOMContentLoaded", loadMachines);