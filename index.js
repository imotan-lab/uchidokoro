
fetch("assets/data/machines.json")
.then(r=>r.json())
.then(data=>{
const list=document.getElementById("machine-list")
data.forEach(m=>{
const li=document.createElement("li")
li.innerHTML=`<a href="machine.html?slug=${m.slug}">${m.name}</a> | <a href="checker.html?slug=${m.slug}">チェッカー</a>`
list.appendChild(li)
})
})
