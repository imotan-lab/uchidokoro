
(async()=>{

const machine = await getMachine()
if(!machine)return

checkerTitle.textContent = machine.name+" チェッカー"

const target = machine.checker.target

checkBtn.onclick=()=>{

const game = Number(gameInput.value)

if(game>=target){
result.textContent=machine.checker.ok
}else{
result.textContent=machine.checker.ng
}

}

})()
