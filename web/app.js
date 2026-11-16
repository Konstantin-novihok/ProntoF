const $ = id => document.getElementById(id);
let token = localStorage.getItem("prontof_token") || "";
let registerMode = false;
let activeVideo = null;
let activeJob = null;
let activeVideoUrl = null;
let languages = {};

async function api(path, options={}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("Authorization", "Bearer " + token);
  if (options.body && !(options.body instanceof Blob) && !(options.body instanceof FormData)) headers.set("Content-Type","application/json");
  const response = await fetch(path,{...options,headers});
  const data = response.status===204 ? null : await response.json().catch(()=>({detail:response.statusText}));
  if (!response.ok) throw new Error(data?.detail || ("HTTP "+response.status));
  return data;
}
function showAuth(){ $("auth").classList.remove("hidden"); $("app").classList.add("hidden"); $("logout").classList.add("hidden"); }
function showApp(user){ $("auth").classList.add("hidden"); $("app").classList.remove("hidden"); $("logout").classList.remove("hidden"); $("user-info").textContent=user.username+" · "+user.role; }
function authMode(){ $("auth-title").textContent=registerMode?"Регистрация":"Вход"; $("auth-submit").textContent=registerMode?"Создать аккаунт":"Войти"; $("auth-switch").textContent=registerMode?"Уже есть аккаунт? Войти":"Создать аккаунт"; $("name-wrap").classList.toggle("hidden",!registerMode); $("password").autocomplete=registerMode?"new-password":"current-password"; }
$("auth-switch").onclick=()=>{registerMode=!registerMode;authMode();$("auth-error").textContent="";};
$("auth-form").onsubmit=async e=>{e.preventDefault();$("auth-error").textContent="";try{const payload={username:$("username").value.trim(),password:$("password").value};if(registerMode)payload.full_name=$("full-name").value.trim();const result=await api(registerMode?"/api/auth/register":"/api/auth/login",{method:"POST",body:JSON.stringify(payload)});token=result.access_token;localStorage.setItem("prontof_token",token);await boot();}catch(err){$("auth-error").textContent=err.message;}};
$("logout").onclick=()=>{token="";localStorage.removeItem("prontof_token");showAuth();};
async function boot(){try{const [user,health]=await Promise.all([api("/api/auth/me"),api("/api/health")]);languages=health.languages||{};showApp(user);fillLanguages();await loadVideos();}catch(e){token="";localStorage.removeItem("prontof_token");showAuth();}}
function fillLanguages(){const opts=Object.entries(languages).map(([code,name])=>`<option value="${code}">${name}</option>`).join("");$("source-language").insertAdjacentHTML("beforeend",opts);$("target-language").innerHTML=opts;}
async function loadVideos(){try{const rows=await api("/api/videos");$("video-count").textContent=rows.length+" файлов";$("empty").classList.toggle("hidden",rows.length>0);$("videos").innerHTML=rows.map(v=>`<article class="video-card"><div class="video-thumb">▶</div><div class="video-meta"><strong>${escapeHtml(v.filename)}</strong><p>${formatBytes(v.size_bytes)} · ${new Date(v.created_at).toLocaleString()}</p><button class="button subtle" data-open="${v.id}">Открыть</button></div></article>`).join("");document.querySelectorAll("[data-open]").forEach(b=>b.onclick=()=>openVideo(b.dataset.open));}catch(e){alert(e.message);}}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function formatBytes(n){if(!n)return "0 Б";const u=["Б","КБ","МБ","ГБ","ТБ"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(i?1:0)+" "+u[i];}
$("refresh").onclick=loadVideos;
$("upload").onclick=async()=>{const file=$("video-file").files[0];if(!file)return alert("Выберите видеофайл.");const max=500*1024**3;if(file.size>max)return alert("Файл превышает программный предел 500 ГБ.");const chunkSize=8*1024*1024;const id=crypto.randomUUID();const count=Math.ceil(file.size/chunkSize);$("upload").disabled=true;$("upload-status").textContent="Подготовка загрузки…";try{for(let i=0;i<count;i++){const blob=file.slice(i*chunkSize,Math.min(file.size,(i+1)*chunkSize));await api(`/api/uploads/${id}/chunks/${i}`,{method:"PUT",body:blob,headers:{"Content-Type":"application/octet-stream"}});const pct=Math.round((i+1)/count*100);$("upload-progress").style.width=pct+"%";$("upload-status").textContent=`Загружено ${pct}% · часть ${i+1} из ${count}`;}const result=await api(`/api/uploads/${id}/complete`,{method:"POST",body:JSON.stringify({filename:file.name,total_chunks:count})});$("upload-status").textContent="Видео добавлено в видеотеку."; $("video-file").value="";await loadVideos();}catch(e){$("upload-status").textContent="Ошибка: "+e.message;}finally{$("upload").disabled=false;}};
async function openVideo(id){try{activeVideo=await api("/api/videos/"+id);activeJob=null;const response=await fetch("/api/videos/"+id+"/source",{headers:{Authorization:"Bearer "+token}});if(!response.ok)throw new Error("Не удалось открыть видео");if(activeVideoUrl)URL.revokeObjectURL(activeVideoUrl);activeVideoUrl=URL.createObjectURL(await response.blob());$("details").classList.remove("hidden");$("detail-title").textContent=activeVideo.filename;$("player").src=activeVideoUrl;$("original-text").value="";$("translated-text").value="";$("job-status").textContent="Видео готово к обработке.";document.querySelector("#details").scrollIntoView({behavior:"smooth"});}catch(e){alert(e.message);}}
$("close-details").onclick=()=>{$("details").classList.add("hidden");$("player").pause();$("player").removeAttribute("src");if(activeVideoUrl)URL.revokeObjectURL(activeVideoUrl);activeVideoUrl=null;};
$("process").onclick=async()=>{if(!activeVideo)return;try{const job=await api("/api/videos/"+activeVideo.id+"/process",{method:"POST",body:JSON.stringify({source_language:$("source-language").value||null,target_language:$("target-language").value})});activeJob=job.id;$("job-status").textContent="Задача поставлена в очередь.";pollJob();}catch(e){$("job-status").textContent=e.message;}};
async function pollJob(){if(!activeJob)return;try{const j=await api("/api/jobs/"+activeJob);$("job-status").textContent=`${j.status} · ${j.progress}% · ${j.message||""}`;if(j.status==="done"){$("original-text").value=j.transcript||"";$("translated-text").value=j.translated_text||"";}else if(j.status==="error")return;else setTimeout(pollJob,4000);}catch(e){$("job-status").textContent=e.message;}}
$("save-text").onclick=async()=>{if(!activeJob)return alert("Сначала выполните распознавание.");try{await api("/api/jobs/"+activeJob+"/edit",{method:"PUT",body:JSON.stringify({transcript:$("original-text").value,translated_text:$("translated-text").value})});$("edit-status").textContent="Текст сохранён. Субтитры обновлены."; }catch(e){$("edit-status").textContent=e.message;}};
$("download-original").onclick=()=>downloadSubtitle("original");
$("download-translated").onclick=()=>downloadSubtitle("translated");
async function downloadSubtitle(kind){if(!activeJob)return alert("Сначала выполните распознавание.");try{const r=await fetch("/api/jobs/"+activeJob+"/subtitles/"+kind,{headers:{Authorization:"Bearer "+token}});if(!r.ok)throw new Error("Субтитры пока недоступны");const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=kind+".srt";a.click();URL.revokeObjectURL(url);}catch(e){$("edit-status").textContent=e.message;}}
authMode();if(token)boot();else showAuth();
