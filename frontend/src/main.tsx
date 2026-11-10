import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

const root = document.getElementById('root')!
root.dataset.theme = localStorage.getItem('agent_theme') || 'dark'

//: How the last interaction arrived: pointer or keyboard.
//:
//: Focus rings are a keyboard affordance, and `:focus-visible` is supposed to
//: know that -- but it does not when focus is moved *by script*, which is what
//: antd does when it hands focus back to the button that opened a modal. The
//: button then wears a 2px ring for the rest of the visit, after a click, and on
//: a solid primary it reads as a second border rather than a focus indicator
//: (measured on the memory toolbar).
//:
//: So the ring is made conditional on how the person is actually working: the
//: stylesheets only draw it when the answer is "keyboard".
document.documentElement.dataset.input = 'pointer'
window.addEventListener(
  'pointerdown',
  () => { document.documentElement.dataset.input = 'pointer' },
  { capture: true, passive: true },
)
window.addEventListener(
  'keydown',
  () => { document.documentElement.dataset.input = 'keyboard' },
  { capture: true, passive: true },
)

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
