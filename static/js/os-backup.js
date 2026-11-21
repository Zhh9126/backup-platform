(function(){
  const $=id=>document.getElementById(id);
  const T=s=>(window.T?window.T(s):s);
  const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const toast=(m,t)=>{try{if(window.BKP&&BKP.toast)BKP.toast(m,t||"info");}catch(e){}};
  const size=n=>{try{return BKP.humanSize?BKP.humanSize(n||0):((n||0)+" B");}catch(e){return (n||0)+" B";}};
  const tm=iso=>{try{return BKP.fmtTime?BKP.fmtTime(iso):(iso||"-");}catch(e){return iso||"-";}};
  const dur=s=>{try{return BKP.fmtDuration?BKP.fmtDuration(s):(s||"-");}catch(e){return s||"-";}};
  let rows=[],timer=null;

  async function api(path,opt){
    const r=await fetch(path,Object.assign({credentials:"same-origin",headers:{"Content-Type":"application/json"}},opt||{}));
    let d={};try{d=await r.json();}catch(e){}
    if(!r.ok||d.ok===false)throw Error(d.error||T("请求失败（HTTP "+r.status+"）"));
    return d;
  }
  function badge(s){
    if(s==="running")return '<span class="badge bg-info text-dark">'+T("执行中")+"</span>";
    if(s==="success")return '<span class="badge bg-success">'+T("成功")+"</span>";
    if(s==="failed")return '<span class="badge bg-danger">'+T("失败")+"</span>";
    return '<span class="badge bg-secondary">'+esc(s||"-")+"</span>";
  }
  async function loadHosts(){
    try{
      const r=await fetch("/api/hosts",{credentials:"same-origin"});
      const d=await r.json();
      const items=Array.isArray(d)?d:(d.items||d.data||[]);
      $("osHost").innerHTML='<option value="">'+T("本机")+'</option><option value="direct">'+T("其他机器")+"</option>"+
        items.map(x=>'<option value="'+x.id+'">'+T("已纳管：")+esc(x.name||x.host_key)+"</option>").join("");
    }catch(e){}
  }
  function render(){
    if(!rows.length){$("osRows").innerHTML="<tr><td colspan=\"10\">"+T("暂无操作系统备份记录")+"</td></tr>";return;}
    $("osRows").innerHTML=rows.map(function(x,i){
      const running=x.status==="running";
      const prog=running?'<div class="small text-muted">'+T("已处理")+" "+(x.progress_files||0)+" "+T("个文件")+" · "+size(x.progress_bytes||0)+"</div>":"";
      const err=x.error_detail?'<div class="small text-danger">'+esc(x.error_detail)+"</div>":"";
      const kind=x.kind==="restore"?'<span class="badge bg-light text-dark border">'+T("恢复")+"</span>":'<span class="badge bg-light text-dark border">'+T("备份")+"</span>";
      const act=(x.status==="success"&&x.archive_path)?'<button class="btn btn-sm btn-outline-primary" data-os="'+i+'">'+T("恢复")+"</button>":'<span class="text-muted small">-</span>';
      const ck=x.checksum?'<code class="small">'+esc(String(x.checksum).slice(0,12))+"</code>":'<span class="text-muted small">-</span>';
      return "<tr>"+
        "<td>"+kind+" "+esc(x.label)+"</td>"+
        '<td class="small"><code class="small d-inline-block text-truncate" style="max-width:190px" title="'+esc(x.source_root||"-")+'">'+esc(x.source_root||"-")+"</code></td>"+
        "<td>"+badge(x.status)+prog+err+"</td>"+
        '<td class="small">'+esc(tm(x.started_at||x.created_at))+"</td>"+
        '<td class="small">'+esc(x.finished_at?tm(x.finished_at):"-")+"</td>"+
        '<td class="small">'+esc(x.duration_sec?dur(x.duration_sec):"-")+"</td>"+
        "<td>"+(x.file_count||0)+"</td>"+
        '<td class="small">'+esc(size(x.size_bytes||0))+"</td>"+
        '<td title="'+esc(x.checksum||"")+'">'+ck+"</td>"+
        "<td>"+act+"</td></tr>";
    }).join("");
  }
  function schedulePoll(){
    if(timer){clearTimeout(timer);timer=null;}
    if(rows.some(x=>x.status==="running"))timer=setTimeout(load,3000);
  }
  async function load(){
    try{
      const d=await api("/api/os-backup/records");
      rows=d.items||[];
      render();schedulePoll();
    }catch(e){}
  }
  $("osRefresh").onclick=load;
  $("osHost").onchange=function(){ $("osRemoteFields").classList.toggle("d-none",$("osHost").value!=="direct"); };
  $("osBackup").onclick=async function(){
    const btn=this;const old=btn.textContent;
    btn.disabled=true;btn.textContent=T("提交中…");
    try{
      const direct=$("osHost").value==="direct";
      const body={source_root:($("osSource").value||"/").trim(),label:$("osLabel").value};
      if(direct)body.ssh_cred={hostname:$("osRemoteHost").value,port:Number($("osRemotePort").value)||22,username:$("osRemoteUser").value,password:$("osRemotePassword").value};
      else body.host_id=$("osHost").value||null;
      const d=await api("/api/os-backup/backup",{method:"POST",body:JSON.stringify(body)});
      toast(d.message||T("备份任务已提交"),"success");
      await load();
    }catch(e){ toast(e.message,"danger"); }
    finally{ btn.disabled=false;btn.textContent=old; }
  };
  $("osRows").onclick=function(e){
    const b=e.target.closest("[data-os]");if(!b)return;
    $("osArchive").value=rows[+b.dataset.os].archive_path;
    $("osPlan").textContent="";
    try{bootstrap.Modal.getOrCreateInstance($("osRestoreModal")).show();}catch(err){}
  };
  $("osPlanBtn").onclick=async function(){
    const btn=this;const old=btn.textContent;btn.disabled=true;
    try{
      const d=await api("/api/os-backup/restore",{method:"POST",body:JSON.stringify({archive_path:$("osArchive").value,target_root:$("osTarget").value,dry_run:true})});
      const r=d.result||{};
      $("osPlan").textContent=T("校验通过：共 "+(r.file_count||0)+" 个文件，目标目录 "+(r.target_root||"-"));
      toast(T("校验通过"),"success");
    }catch(e){ $("osPlan").textContent=e.message; toast(e.message,"danger"); }
    finally{ btn.disabled=false;btn.textContent=old; }
  };
  $("osRestoreBtn").onclick=async function(){
    if(!confirm(T("确认恢复到指定目标目录？")))return;
    const btn=this;const old=btn.textContent;btn.disabled=true;
    try{
      const d=await api("/api/os-backup/restore",{method:"POST",body:JSON.stringify({archive_path:$("osArchive").value,target_root:$("osTarget").value,dry_run:false,confirm:true})});
      toast(d.message||T("恢复任务已提交"),"success");
      $("osPlan").textContent=T("恢复任务已提交（记录 #"+(d.id||"-")+"），完成后可在下方记录列表查看结果。");
      await load();
    }catch(e){ $("osPlan").textContent=e.message; toast(e.message,"danger"); }
    finally{ btn.disabled=false;btn.textContent=old; }
  };
  $("osJobBtn").onclick=async function(){
    const btn=this;btn.disabled=true;
    try{
      const d=await api("/api/os-backup/restore-plan?archive_path="+encodeURIComponent($("osArchive").value));
      const plan=d.plan||{};
      const ok=(plan.platform_scope||[]).map(x=>"&#10003; "+esc(x)).join("<br>");
      const no=(plan.not_supported||[]).map(x=>"&#10007; "+esc(x)).join("<br>");
      $("osJobScope").innerHTML="<div class='mb-1'><strong>"+T("平台自动完成")+"</strong></div>"+ok+
        "<hr class='my-2'><div class='mb-1'><strong>"+T("平台不提供")+"</strong></div>"+no;
      $("osJobSteps").innerHTML=(plan.steps||[]).map(function(s,i){
        return "<tr><td>"+(i+1)+"</td><td><strong>"+esc(s.title)+"</strong>"+
          (s.manual?' <span class="badge bg-warning text-dark">'+T("需人工")+"</span>":"")+
          '<div class="small text-muted">'+esc(s.detail||"")+"</div></td>"+
          '<td><code class="small">'+esc(s.command||T("在救援介质中按说明执行"))+"</code></td></tr>";
      }).join("");
      $("osJobScript").textContent=d.script||"";
      try{bootstrap.Modal.getOrCreateInstance($("osJobModal")).show();}catch(e){}
    }catch(e){ toast(e.message,"danger"); }
    finally{ btn.disabled=false; }
  };
  $("osJobCopy").onclick=function(){
    const txt=$("osJobScript").textContent||"";
    if(window.copyText){try{window.copyText(txt);}catch(e){}}
    else if(navigator.clipboard){try{navigator.clipboard.writeText(txt);}catch(e){}}
    else{const ta=document.createElement("textarea");ta.value=txt;document.body.appendChild(ta);ta.select();
         try{document.execCommand("copy");}catch(e){} document.body.removeChild(ta);}
    toast(T("恢复脚本已复制"),"success");
  };
  loadHosts();load();
})();
