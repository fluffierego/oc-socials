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


// OC Socials saved-like button handler
document.addEventListener("click", async (event) => {
  const button = event.target.closest(".like-button");
  if (!button || button.disabled) return;

  const postId = button.dataset.post;
  button.disabled = true;

  try {
    const response = await fetch("/api/like", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({post_id: postId})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not like post.");

    document.querySelectorAll(
      '.like-button[data-post="' + postId + '"]'
    ).forEach((b) => {
      b.classList.toggle("liked", data.liked);
      const heart = b.querySelector(".like-heart");
      const label = b.querySelector(".like-label");
      if (heart) heart.textContent = data.liked ? "♥" : "♡";
      if (label) label.textContent = data.liked ? "Liked" : "Like";
    });

    document.querySelectorAll(
      '[data-like-count="' + postId + '"]'
    ).forEach((el) => {
      el.textContent = data.count + (data.count === 1 ? " like" : " likes");
    });
  } catch (error) {
    alert(error.message || "Could not like post.");
  } finally {
    button.disabled = false;
  }
});
