
(async()=>{

const machine = await getMachine()
if(!machine)return

document.title = machine.seo.title

machineTitle.textContent = machine.name
machineInfo.textContent = machine.info
strategy.textContent = machine.strategy

checkerLink.href="checker.html?slug="+machine.slug

})()
