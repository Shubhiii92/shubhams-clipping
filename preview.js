const download = document.querySelector('#download')
for (const format of document.querySelectorAll('input[name="format"]')) {
  format.addEventListener('change', () => {
    if (format.checked) download.download = `shubhams-clipping-${format.value}.mp4`
  })
}
