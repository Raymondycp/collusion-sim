;(function () {
  var stored = null
  try { stored = localStorage.getItem('theme') } catch (e) {}
  var dark = stored ? stored === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches
  document.documentElement.classList.toggle('dark', dark)
})()
