document.querySelectorAll(".reply-form").forEach(form => {
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    const res = await fetch("/api/reply", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({post_id:form.dataset.post, oc_id:fd.get("oc_id"), text:fd.get("text")})
    });
    if(res.ok) location.reload();
    else alert((await res.json()).error || "Could not reply.");
  });
});
const mf=document.getElementById("message-form");
if(mf){
  mf.addEventListener("submit", async e=>{
    e.preventDefault();
    const res=await fetch("/api/message",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        chat_id:document.getElementById("chat-id").value,
        oc_id:document.getElementById("oc-id").value,
        text:document.getElementById("message-text").value
      })
    });
    if(res.ok) location.reload();
    else alert((await res.json()).error || "Could not send message.");
  });
}
