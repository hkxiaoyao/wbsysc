import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App.jsx'
import TenantApp from './TenantApp.jsx'
import './index.css'

const RootApp = window.location.pathname.startsWith('/tenant/ui') ? TenantApp : App

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN}>
      <RootApp />
    </ConfigProvider>
  </React.StrictMode>,
)
