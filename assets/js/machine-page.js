(async()=>{

const machine = await getMachine()
if(!machine)return

document.title = machine.seo?.title || machine.name

document.getElementById("machine-title").textContent = machine.name
document.getElementById("machine-info").textContent = machine.info || ""
document.getElementById("strategy").textContent = machine.strategy || ""

document.getElementById("checker-link").href =
"checker.html?slug="+machine.slug

})()
