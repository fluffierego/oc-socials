(() => {
  const viewer = document.getElementById('story-viewer');
  if (!viewer) return;
  const mediaSlot = viewer.querySelector('.story-viewer-media-content');
  const textSlot = viewer.querySelector('.story-viewer-text');
  const mediaFrame = viewer.querySelector('.story-viewer-media');
  const form = viewer.querySelector('.story-reply-form');
  const status = viewer.querySelector('.story-reply-status');
  const bubbles = Array.from(document.querySelectorAll('.story-bubble'));
  let groupIndex = -1, slideIndex = 0, slides = [], storyId = null;

  function renderSlide() {
    if (!slides.length || !slides[slideIndex]) return;
    const slide = slides[slideIndex];
    storyId = slide.id;
    const oldVideo = mediaSlot.querySelector('video');
    if (oldVideo) oldVideo.pause();
    mediaSlot.replaceChildren();
    const media = document.createElement(slide.media_type === 'video' ? 'video' : 'img');
    media.src = slide.media_url;
    if (slide.media_type === 'video') { media.autoplay = true; media.playsInline = true; media.controls = true; }
    else media.alt = 'Instagram Story';
    mediaSlot.appendChild(media);
    textSlot.textContent = slide.text_overlay || '';
    textSlot.style.color = slide.text_color || '#fff';
    textSlot.hidden = !textSlot.textContent.trim();
    const bubble = bubbles[groupIndex];
    viewer.querySelector('.story-account-avatar').src = bubble.dataset.storyAvatar || '';
    viewer.querySelector('.story-account-name').textContent = bubble.dataset.storyName || '';
    const progress = viewer.querySelector('.story-progress');
    progress.replaceChildren();
    slides.forEach((_, i) => {
      const bar = document.createElement('span');
      bar.className = i < slideIndex ? 'done' : (i === slideIndex ? 'current' : '');
      progress.appendChild(bar);
    });
    status.textContent = '';
  }

  function openGroup(index, resetForm = true) {
    if (index < 0 || index >= bubbles.length) { closeViewer(); return; }
    groupIndex = index;
    try { slides = JSON.parse(bubbles[groupIndex].dataset.storySlides || '[]'); }
    catch (_) { slides = []; }
    if (!slides.length) return;
    slideIndex = 0;
    if (resetForm) form.reset();
    renderSlide();
    viewer.hidden = false;
  }
  function move(direction) {
    if (direction > 0 && slideIndex < slides.length - 1) {
      slideIndex++; renderSlide(); return;
    }
    if (direction < 0 && slideIndex > 0) {
      slideIndex--; renderSlide(); return;
    }
    openGroup(groupIndex + direction, false);
  }
  function closeViewer() {
    const video = mediaSlot.querySelector('video'); if (video) video.pause();
    viewer.hidden = true; mediaSlot.replaceChildren(); textSlot.textContent = ''; textSlot.hidden = true;
    storyId = null; slides = []; groupIndex = -1; slideIndex = 0;
  }
  bubbles.forEach((bubble, index) => bubble.addEventListener('click', () => openGroup(index)));
  viewer.querySelector('.story-close').addEventListener('click', closeViewer);
  viewer.addEventListener('click', e => {
    if (e.target === viewer) closeViewer();
  });
  mediaFrame.addEventListener('click', e => {
    if (e.target.closest('button, video, input, select, form')) return;
    const rect = mediaFrame.getBoundingClientRect();
    move(e.clientX < rect.left + rect.width / 2 ? -1 : 1);
  });
  document.addEventListener('keydown', e => {
    if (viewer.hidden) return;
    if (e.key === 'ArrowRight') move(1);
    else if (e.key === 'ArrowLeft') move(-1);
    else if (e.key === 'Escape') closeViewer();
  });
  viewer.querySelector('.story-heart').addEventListener('click', e => {
    e.currentTarget.textContent = e.currentTarget.textContent.trim() === '♡' ? '♥' : '♡';
  });
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!storyId) { status.textContent = 'Story unavailable.'; return; }
    const data = new FormData(form); status.textContent = 'Sending…';
    try {
      const response = await fetch(`/api/story/${encodeURIComponent(storyId)}/reply`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({oc_id:data.get('oc_id'), text:data.get('text')})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not send reply.');
      status.textContent = 'Reply sent!';
      if (result.conversation_url) window.location.href = result.conversation_url;
    } catch (err) { status.textContent = err.message || 'Could not send reply.'; }
  });
})();