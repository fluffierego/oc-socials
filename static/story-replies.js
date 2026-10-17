(() => {
  const viewer = document.getElementById('story-viewer');
  if (!viewer) return;
  const mediaSlot = viewer.querySelector('.story-viewer-media-content');
  const textSlot = viewer.querySelector('.story-viewer-text');
  const form = viewer.querySelector('.story-reply-form');
  const status = viewer.querySelector('.story-reply-status');
  let storyId = null;
  document.querySelectorAll('.story-bubble').forEach(bubble => bubble.addEventListener('click', () => {
    storyId = bubble.dataset.storyId;
    mediaSlot.replaceChildren();
    const type = bubble.dataset.storyType;
    const media = document.createElement(type === 'video' ? 'video' : 'img');
    media.src = bubble.dataset.storyMedia;
    if (type === 'video') { media.autoplay = true; media.playsInline = true; media.controls = true; }
    else media.alt = 'Instagram Story';
    mediaSlot.appendChild(media);
    textSlot.textContent = bubble.dataset.storyText || '';
    textSlot.style.color = bubble.dataset.storyColor || '#fff';
    textSlot.hidden = !textSlot.textContent.trim();
    const avatar = viewer.querySelector('.story-account-avatar');
    if (avatar) avatar.src = bubble.querySelector('img')?.src || '';
    const name = viewer.querySelector('.story-account-name');
    if (name) name.textContent = bubble.querySelector('span')?.textContent || '';
    status.textContent = '';
    form.reset();
    viewer.hidden = false;
  }));
  function closeViewer() {
    const video = mediaSlot.querySelector('video'); if (video) video.pause();
    viewer.hidden = true; mediaSlot.replaceChildren(); textSlot.textContent = ''; textSlot.hidden = true; storyId = null;
  }
  viewer.querySelector('.story-close').addEventListener('click', closeViewer);
  viewer.addEventListener('click', e => { if (e.target === viewer) closeViewer(); });
  viewer.querySelector('.story-heart').addEventListener('click', e => { e.currentTarget.textContent = e.currentTarget.textContent.trim() === '♡' ? '♥' : '♡'; });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!storyId) { status.textContent = 'Story unavailable.'; return; }
    const data = new FormData(form); status.textContent = 'Sending…';
    try {
      const response = await fetch(`/api/story/${encodeURIComponent(storyId)}/reply`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({oc_id:data.get('oc_id'), text:data.get('text')})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not send reply.');
      status.textContent = 'Reply sent!';
      if (result.conversation_url) window.location.href = result.conversation_url;
    } catch (err) { status.textContent = err.message || 'Could not send reply.'; }
  });
})();
