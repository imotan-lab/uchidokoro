
async function getMachine(){

const params = new URLSearchParams(location.search)
const slug = params.get("slug")

if(!slug){
document.body.innerHTML="slugがありません"
return null
}

const res = await fetch("assets/data/machines.json")
const data = await res.json()

const machine = data.find(m=>m.slug===slug)

if(!machine){
document.body.innerHTML="機種が見つかりません"
return null
}

return machine
}
