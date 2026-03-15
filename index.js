:root{
  --bg:#0c0c0e;
  --surface:#111115;
  --surface2:#18181e;
  --surface3:#15151a;
  --border:#252531;
  --accent:#c8972a;
  --accent2:#e8b84a;
  --text:#ddd8cc;
  --text-sub:#aaa49a;
  --text-muted:#7a7870;
  --good:#4a9e6a;
  --warn:#c8972a;
  --bad:#9e4a4a;
  --shadow:0 18px 42px rgba(0,0,0,.28);
  --radius:16px;
  --content-wide:1100px;
  --content-narrow:600px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;scroll-behavior:smooth}
body{
  background:var(--bg);
  color:var(--text);
  font-family:"Noto Sans JP",-apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN","Yu Gothic","Meiryo",sans-serif;
  line-height:1.8;
  overflow-x:hidden;
}
a{color:var(--accent2);text-decoration:none}
a:hover{text-decoration:none;opacity:.92}
img{max-width:100%;display:block}
button,input,select,textarea{font:inherit}

.site-header{
  position:sticky;
  top:0;
  z-index:100;
  background:rgba(12,12,14,.94);
  border-bottom:1px solid var(--border);
  backdrop-filter:blur(10px);
}
.site-header-inner,
.page,
.site-footer-inner{
  width:min(100%, calc(var(--content-wide) + 32px));
  margin:0 auto;
  padding-left:16px;
  padding-right:16px;
}
.site-header-inner{
  min-height:56px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
}
.brand{
  font-family:"Shippori Mincho",serif;
  font-size:1.06rem;
  font-weight:800;
  color:var(--accent);
  letter-spacing:.08em;
}
.header-nav{display:flex;gap:16px;flex-wrap:wrap}
.header-nav a,.back-link,.shell-meta{font-size:.78rem;color:var(--text-muted)}
.page{padding-top:28px;padding-bottom:52px}
.hero{margin-bottom:20px}
.eyebrow{
  display:inline-block;
  margin-bottom:10px;
  color:var(--accent);
  letter-spacing:.22em;
  font-size:.64rem;
  font-weight:700;
}
.page-title{
  font-family:"Shippori Mincho",serif;
  font-size:clamp(1.5rem,4vw,2.4rem);
  line-height:1.45;
  margin:0 0 12px;
}
.page-title em{color:var(--accent);font-style:normal}
.lead,.lead-inline{color:var(--text-sub);font-size:.92rem;margin:0;line-height:1.9}
.page-narrow{width:min(100%, calc(var(--content-narrow) + 32px))}
.grid{display:grid;gap:20px}
.grid-2{grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr)}
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:14px;
  box-shadow:var(--shadow);
  padding:18px;
}
.card h2,.card h3{margin:0 0 12px;line-height:1.5}
.section-title{font-size:1rem;color:var(--accent2);margin:0 0 8px}
.meta-list,.point-list{margin:0;padding-left:1.2rem}
.meta-list li,.point-list li{margin:6px 0;color:var(--text-sub)}
.notice{border-radius:12px;padding:13px 14px;margin-top:14px;font-weight:700}
.notice-ok{background:rgba(74,158,106,.12);color:#9fd0b3;border:1px solid rgba(74,158,106,.25)}
.notice-warn{background:rgba(200,151,42,.1);color:#f0d391;border:1px solid rgba(200,151,42,.22)}
.search-wrap{min-width:min(100%,280px)}
.search-label,label{display:block;font-size:.78rem;font-weight:700;margin-bottom:7px;color:var(--text)}
input[type="number"],input[type="search"],select,textarea{
  width:100%;
  min-height:46px;
  border:1px solid var(--border);
  border-radius:10px;
  padding:0 14px;
  font-size:1rem;
  background:var(--surface2);
  color:var(--text);
  outline:none;
}
textarea{padding-top:12px;padding-bottom:12px;min-height:120px}
input[type="number"]:focus,input[type="search"]:focus,select:focus,textarea:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(200,151,42,.12);
}
.btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-height:44px;
  border:none;
  border-radius:10px;
  padding:12px 16px;
  background:linear-gradient(135deg,var(--accent) 0%, #a07320 100%);
  color:#0c0c0e;
  font-size:.9rem;
  font-weight:800;
  cursor:pointer;
  transition:opacity .15s ease, transform .05s ease;
}
.btn:hover{opacity:.92}
.btn:active{transform:translateY(1px)}
.btn-sub{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.cta-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
.result-box{min-height:64px;border:1px dashed var(--border);border-radius:12px;padding:14px;background:var(--surface2)}
.result-text{margin:0;font-size:1rem;font-weight:800}
.site-footer{border-top:1px solid var(--border);background:transparent}
.site-footer-inner{padding-top:24px;padding-bottom:32px;color:var(--text-muted);font-size:.74rem;text-align:center}

.index-toolbar{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:20px}
.machine-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.machine-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);padding:16px;display:flex;flex-direction:column;gap:12px}
.machine-card-head{display:grid;gap:8px}
.machine-badge{display:inline-flex;align-items:center;width:max-content;padding:4px 8px;border:1px solid rgba(200,151,42,.22);border-radius:999px;color:var(--accent);font-size:.62rem;font-weight:700;letter-spacing:.12em}
.machine-title{margin:0;font-size:1rem;line-height:1.5;color:var(--text)}
.machine-slug{margin:0;color:var(--text-muted);font-size:.72rem;word-break:break-word}
.machine-links{display:flex;gap:10px;flex-wrap:wrap}
.machine-links .btn{flex:1 1 130px}
.empty-message{text-align:center;color:var(--text-muted)}

.template-shell,.checker-shell{width:min(100%, calc(var(--content-narrow) + 32px));margin:0 auto}
.template-hero,.checker-hero{
  padding:72px 16px 28px;
  border-bottom:1px solid var(--border);
}
.template-body,.checker-body{padding:20px 16px 0}
.template-toc{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;margin:18px 16px 0}
.template-toc-title{font-size:.72rem;color:var(--accent);font-weight:700;letter-spacing:.12em;margin-bottom:10px}
.template-toc a{display:block;padding:7px 0;border-bottom:1px solid var(--border);color:var(--text-sub);font-size:.8rem}
.template-toc a:last-child{border-bottom:none}
.template-link-card,.checker-link-card{display:flex;align-items:center;gap:14px;margin:14px 16px 0;padding:16px;border:1px solid var(--accent);border-radius:14px;background:linear-gradient(135deg,#1a1508 0%,#111115 100%)}
.template-link-body,.checker-link-body{flex:1;min-width:0}
.template-link-label,.checker-link-label{font-size:.58rem;color:var(--accent);font-weight:700;letter-spacing:.15em;margin-bottom:3px}
.template-link-title,.checker-link-title{font-size:.92rem;font-weight:700;color:var(--text);margin-bottom:2px}
.template-link-desc,.checker-link-desc{font-size:.74rem;color:var(--text-muted);line-height:1.7}
.template-section{padding-top:32px}
.template-section-title{display:flex;align-items:center;gap:8px;padding-bottom:10px;border-bottom:2px solid var(--accent);margin:0 0 18px;font-size:1.08rem;color:var(--text);font-family:"Shippori Mincho",serif}
.template-note{font-size:.76rem;color:var(--text-muted);background:var(--surface);border-left:3px solid var(--accent);padding:10px 12px;border-radius:0 6px 6px 0}
.template-table{width:100%;border-collapse:collapse;font-size:.78rem}
.template-table th{background:var(--surface2);color:var(--text-muted);font-size:.65rem;font-weight:700;letter-spacing:.05em;padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
.template-table td{padding:10px;border-bottom:1px solid var(--border);color:var(--text);vertical-align:top;line-height:1.75}
.template-table tr:last-child td{border-bottom:none}
.template-cards{display:grid;gap:10px}
.template-mini-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px}
.template-mini-card.is-highlight{border-color:var(--accent);background:linear-gradient(135deg,#1a1508 0%,#111115 100%)}
.template-mini-title{font-size:.86rem;font-weight:700;color:var(--text);margin-bottom:4px}
.template-mini-desc{font-size:.75rem;color:var(--text-muted);line-height:1.8}

.legacy-shell{width:min(100%, calc(var(--content-narrow) + 32px));margin:0 auto}
.legacy-page{padding-top:12px}
.legacy-topbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px;padding:0 16px}
.legacy-frame-wrap{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden;box-shadow:var(--shadow)}
.legacy-frame{width:100%;min-height:70vh;border:0;display:block;background:var(--bg)}
.legacy-loading,.legacy-error{padding:18px;background:var(--surface);border:1px solid var(--border);border-radius:14px;color:var(--text-sub)}
.legacy-error{border-color:rgba(158,74,74,.4);color:#e1b0b0}

@media (max-width:900px){
  .machine-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .grid-2{grid-template-columns:1fr}
}
@media (max-width:640px){
  .site-header-inner,.index-toolbar{align-items:flex-start;flex-direction:column}
  .machine-grid{grid-template-columns:1fr}
  .machine-links .btn{flex:1 1 100%}
}
