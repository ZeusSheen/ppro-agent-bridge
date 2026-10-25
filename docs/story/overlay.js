/* The story floats over the page. It opens on a visitor's first visit (?story=1 always opens it); Skip, Esc,
   a click on the dark margin or the last card's button closes it, and it stays closed for that visitor. The
   button under "Why I built this" opens it again. While it is open the page behind does not scroll, focus
   stays inside it, and a hairline along the top of the sheet shows how far along you are.
   It tells story.js with two events on the overlay: storyoverlay:open and storyoverlay:close. */
(function () {
  var overlay = document.getElementById('story-overlay');
  if (!overlay) return;
  var scroller = overlay.querySelector('.story-scroller');
  var story = overlay.querySelector('.story');
  var bar = overlay.querySelector('.story-progress i');
  var KEY = 'ppro-bridge.story-1776';
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var lastFocus = null, closeTimer = 0, ticking = false;

  function seen() { try { return localStorage.getItem(KEY) === 'closed'; } catch (e) { return false; } }
  function remember() { try { localStorage.setItem(KEY, 'closed'); } catch (e) {} }
  function emit(name) { overlay.dispatchEvent(new CustomEvent(name, { bubbles: true })); }

  function progress() {
    ticking = false;
    var max = scroller.scrollHeight - scroller.clientHeight;
    var p = max > 0 ? Math.min(1, scroller.scrollTop / max) : 0;
    if (bar) bar.style.transform = 'scaleX(' + p.toFixed(4) + ')';
    story.classList.toggle('scrolled', scroller.scrollTop > 24);
  }
  scroller.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(progress); }
  }, { passive: true });

  // the scroller takes focus, so Space, Page Down and the arrows move the story (Tab reaches Skip)
  scroller.setAttribute('tabindex', '-1');

  function open() {
    clearTimeout(closeTimer);
    if (!overlay.hidden && overlay.classList.contains('shown')) return;
    if (overlay.hidden) lastFocus = document.activeElement;
    overlay.hidden = false;
    document.documentElement.classList.add('story-open');
    scroller.scrollTop = 0;
    progress();
    void overlay.offsetWidth;  // fade in from 0
    overlay.classList.add('shown');
    scroller.focus({ preventScroll: true });
    emit('storyoverlay:open');
  }

  function close() {
    if (overlay.hidden || !overlay.classList.contains('shown')) return;
    remember();
    overlay.classList.remove('shown');
    emit('storyoverlay:close');
    closeTimer = setTimeout(function () {
      overlay.hidden = true;
      document.documentElement.classList.remove('story-open');
      if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
    }, reduce ? 0 : 240);
  }

  overlay.addEventListener('click', function (e) {
    if (e.target.closest('[data-story-close]')) { close(); return; }
    if (!e.target.closest('.sheet')) close();  // the dark margin around the sheet
  });

  document.addEventListener('keydown', function (e) {
    if (overlay.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key !== 'Tab') return;
    var stops = [].slice.call(overlay.querySelectorAll('a[href], button')).filter(function (b) {
      return b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden';
    });
    if (!stops.length) return;
    var first = stops[0], last = stops[stops.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === scroller)) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  });

  document.querySelectorAll('[data-story-open]').forEach(function (b) { b.addEventListener('click', open); });

  window.StoryOverlay = { open: open, close: close, isOpen: function () { return !overlay.hidden; } };
  if (!seen() || /[?&]story=1(&|$)/.test(location.search)) open();
})();
