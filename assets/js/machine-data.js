async function getMachine(){

const params = new URLSearchParams(location.search)
const slug = params.get("slug")

const res = await fetch("assets/data/machines.json")
const data = await res.json()

return data.find(m=>m.slug===slug)

}
