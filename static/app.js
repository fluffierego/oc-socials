document.querySelectorAll(".reply-form").forEach(form => {
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(form);
    const res = await fetch("/api/reply", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({post_id:form.dataset.post, oc_id:fd.get("oc_id"), text:fd.get("text"), parent_id:fd.get("parent_id") || null})
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
      body: JSON.stringify({
        post_id: postId,
        oc_id: (document.getElementById("instagram-oc-switcher") || document.getElementById("twitter-oc-switcher"))?.value || null
      })
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


// OC Socials: choose which comment a reply should respond to.
document.addEventListener("click", (event) => {
  const trigger = event.target.closest(".comment-reply-trigger");
  if (!trigger) return;
  const post = trigger.closest(".post, .tweet");
  if (!post) return;
  const form = post.querySelector(".reply-form");
  if (!form) return;
  const parentField = form.querySelector('[name="parent_id"]');
  const textField = form.querySelector('input[name="text"]');
  if (!parentField || !textField) return;
  parentField.value = trigger.dataset.parentId || "";
  textField.placeholder = "Reply to @" + (trigger.dataset.username || "user") + "...";
  textField.focus();
  form.scrollIntoView({behavior: "smooth", block: "nearest"});
});


// OC Socials: saved comment likes
 document.addEventListener("click", async (event) => {
  const button = event.target.closest(".reply-like-button");
  if (!button || button.disabled) return;
  const replyId = button.dataset.replyId;
  button.disabled = true;
  try {
    const response = await fetch("/api/reply-like", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reply_id: replyId})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not like comment.");
    document.querySelectorAll('.reply-like-button[data-reply-id="' + replyId + '"]').forEach((item) => {
      item.classList.toggle("liked", data.liked);
      item.setAttribute("aria-pressed", data.liked ? "true" : "false");
      const heart = item.querySelector(".reply-heart");
      const count = item.querySelector(".reply-like-count");
      if (heart) heart.textContent = data.liked ? "♥" : "♡";
      if (count) count.textContent = data.count ? String(data.count) : "";
    });
  } catch (error) {
    alert(error.message || "Could not like comment.");
  } finally {
    button.disabled = false;
  }
});

// OC Socials: Share copies a direct link to the post's anchor.
async function ocSocialsCopyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("Clipboard access was blocked by the browser.");
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".share-post-button");
  if (!button || button.disabled) return;
  const link = new URL(window.location.href);
  link.hash = "post-" + button.dataset.post;
  const originalText = button.dataset.originalText || button.textContent;
  button.dataset.originalText = originalText;
  button.disabled = true;
  try {
    await ocSocialsCopyText(link.href);
    button.textContent = "✓ Link copied";
    button.setAttribute("aria-label", "Post link copied");
  } catch (error) {
    alert("Couldn't copy the post link automatically. You can copy this link:\n" + link.href);
  } finally {
    button.disabled = false;
  }
});


// OC Socials: saved retweets for Twitter/X
 document.addEventListener("click", async (event) => {
  const button = event.target.closest(".retweet-button");
  if (!button || button.disabled) return;
  const postId = button.dataset.postId;
  button.disabled = true;
  try {
    const response = await fetch("/api/retweet", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({post_id: postId, oc_id: document.getElementById("twitter-oc-switcher") ? document.getElementById("twitter-oc-switcher").value : null})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not retweet this post.");
    document.querySelectorAll('.retweet-button[data-post-id="' + postId + '"]').forEach((item) => {
      item.classList.toggle("retweeted", data.retweeted);
      item.setAttribute("aria-pressed", data.retweeted ? "true" : "false");
      const label = item.querySelector(".retweet-label");
      const count = item.querySelector(".retweet-count");
      if (label) label.textContent = data.retweeted ? "Retweeted" : "Retweet";
      if (count) count.textContent = String(data.count);
    });
  } catch (error) {
    alert(error.message || "Could not retweet this post.");
  } finally {
    button.disabled = false;
  }
});

// Instagram identity switcher: likes are performed by the selected OC.
(() => {
  const switcher = document.getElementById("instagram-oc-switcher");
  if (!switcher) return;
  switcher.addEventListener("change", () => {
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("oc_id", switcher.value);
    window.location.assign(nextUrl.toString());
  });
})();

// Twitter/X identity switcher: each Like and Retweet is performed by the selected OC.
(() => {
  const switcher = document.getElementById("twitter-oc-switcher");
  if (!switcher) return;
  switcher.addEventListener("change", () => {
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("oc_id", switcher.value);
    window.location.assign(nextUrl.toString());
  });
})();


// OC Socials: owner-only post deletion
 document.addEventListener("click", async (event) => {
  const button = event.target.closest(".delete-post-button");
  if (!button || button.disabled) return;
  if (!window.confirm("Delete this post? This cannot be undone.")) return;

  button.disabled = true;
  try {
    const response = await fetch("/api/post/delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({post_id: button.dataset.postId})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not delete this post.");
    const card = button.closest("article");
    if (card) card.remove();
  } catch (error) {
    alert(error.message || "Could not delete this post.");
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});

// Instagram carousel controls: each post keeps its own current slide.
document.querySelectorAll('[data-carousel]').forEach(carousel => {
  const slides = [...carousel.querySelectorAll('.carousel-slide')];
  if (slides.length < 2) return;
  let index = 0;
  const dots = [...carousel.querySelectorAll('.carousel-dot')];
  const counter = carousel.querySelector('.carousel-counter');
  const show = next => {
    index = (next + slides.length) % slides.length;
    slides.forEach((slide, i) => {
      slide.classList.toggle('hidden', i !== index);
      const video = slide.querySelector('video');
      if (video && i !== index) video.pause();
    });
    dots.forEach((dot, i) => dot.classList.toggle('active', i === index));
    if (counter) counter.textContent = `${index + 1}/${slides.length}`;
  };
  carousel.querySelector('.carousel-prev')?.addEventListener('click', () => show(index - 1));
  carousel.querySelector('.carousel-next')?.addEventListener('click', () => show(index + 1));
});

// Story viewer supports image/video backgrounds with readable wrapped text overlays.
const storyViewer = document.getElementById('story-viewer');
if (storyViewer) {
  const mediaSlot = storyViewer.querySelector('.story-viewer-media');
  const textSlot = storyViewer.querySelector('.story-viewer-text');
  document.querySelectorAll('.story-bubble').forEach(bubble => bubble.addEventListener('click', () => {
    mediaSlot.replaceChildren();
    const type = bubble.dataset.storyType;
    const media = document.createElement(type === 'video' ? 'video' : 'img');
    media.src = bubble.dataset.storyMedia;
    if (type === 'video') { media.controls = true; media.autoplay = true; media.playsInline = true; }
    else media.alt = 'Instagram Story';
    mediaSlot.appendChild(media);
    textSlot.textContent = bubble.dataset.storyText || '';
    textSlot.style.color = bubble.dataset.storyColor || '#fff';
    textSlot.hidden = !textSlot.textContent;
    storyViewer.hidden = false;
  }));
  storyViewer.querySelector('.story-close').addEventListener('click', () => {
    const video = mediaSlot.querySelector('video'); if (video) video.pause();
    storyViewer.hidden = true; mediaSlot.replaceChildren();
  });
  storyViewer.addEventListener('click', e => { if (e.target === storyViewer) storyViewer.querySelector('.story-close').click(); });
}
