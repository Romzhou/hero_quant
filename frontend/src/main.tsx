/**
 * 前端入口（main）
 * - React 19 + ReactDOM.createRoot 挂载到 #root
 * - BrowserRouter 提供路由上下文，供 App 内 Routes/NavLink 使用
 * - StrictMode 仅开发期生效，用于暴露副作用与不安全生命周期
 */
import React from "react"
import ReactDOM from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import App from "./App"
import "./index.css"

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
)
