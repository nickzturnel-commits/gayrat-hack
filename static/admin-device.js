(() => {
  'use strict';
  const query = window.matchMedia('(max-width: 760px), (pointer: coarse)');
  const current = document.body.classList.contains('mobile-ui') ? 'mobile' : 'desktop';
  const redirectIfNeeded = () => {
    const desired = query.matches ? 'mobile' : 'desktop';
    if (desired !== current) {
      window.location.replace(`/admin/${desired}${window.location.search}${window.location.hash}`);
    }
  };
  redirectIfNeeded();
  if (query.addEventListener) query.addEventListener('change', redirectIfNeeded);
  else query.addListener(redirectIfNeeded);
})();
