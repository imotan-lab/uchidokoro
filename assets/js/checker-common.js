async function loadChecker(machine) {
  const res = await fetch(`assets/data/checkers/${machine}.json`);
  const data = await res.json();

  const btn = document.getElementById("checkBtn");
  const input = document.getElementById("gameInput");
  const result = document.getElementById("result");

  btn.onclick = () => {
    const game = parseInt(input.value || 0, 10);
    if (game >= data.target) {
      result.textContent = "狙い目ラインに到達しています。";
    } else {
      result.textContent = "まだ狙い目ラインに届いていません。";
    }
  };
}