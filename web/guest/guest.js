const form = document.querySelector('#upload')
const video = document.querySelector('#video')
const status = document.querySelector('#status')
const download = document.querySelector('#download')
const button = form.querySelector('button')
let currentPoll

async function showJob(id) {
  const response = await fetch(`/jobs/${id}`)
  const result = await response.json()
  if (!response.ok || result.status === 'error') {
    status.textContent = result.detail || result.message
    button.disabled = false
    return
  }
  if (result.status === 'done') {
    status.textContent = 'Your clip is ready.'
    download.href = `/jobs/${id}/download`
    download.hidden = false
    button.disabled = false
    return
  }
  status.textContent = 'Finding a highlight and rendering your clip…'
  currentPoll = setTimeout(() => pollJob(id), 3000)
}

async function pollJob(id) {
  try {
    await showJob(id)
  } catch {
    status.textContent = 'Reconnecting to the studio…'
    currentPoll = setTimeout(() => pollJob(id), 5000)
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  clearTimeout(currentPoll)
  const file = video.files[0]
  if (!file || file.size > 40 * 1024 * 1024) {
    status.textContent = 'Choose an MP4 under 40 MB.'
    return
  }
  button.disabled = true
  download.hidden = true
  status.textContent = 'Uploading your video…'
  const format = new FormData(form).get('format')
  try {
    const response = await fetch('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'video/mp4', 'X-Clip-Format': format },
      body: file,
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail)
    history.replaceState(null, '', `#${result.id}`)
    await pollJob(result.id)
  } catch (error) {
    status.textContent = error.message || 'Upload failed. Try again.'
    button.disabled = false
  }
})

if (/^#[a-f0-9]{48}$/.test(location.hash)) {
  button.disabled = true
  pollJob(location.hash.slice(1))
}
