import { Card, Form, Input, Button, message } from 'antd'
import { LockOutlined } from '@ant-design/icons'
import api from '../api.js'

export default function Login({ onLogin }) {
  const [form] = Form.useForm()
  const submit = async (v) => {
    try {
      const r = await api.post('/admin/login', { password: v.password })
      onLogin(r.data.token)
    } catch (e) {
      message.error(e.response?.data?.detail || '登录失败')
    }
  }
  return (
    <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f0f2f5' }}>
      <Card title="管理后台登录" style={{ width: 360 }}>
        <Form form={form} onFinish={submit}>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="管理员密码" size="large" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block size="large">登录</Button>
        </Form>
        <div style={{ marginTop: 12, color: '#999', fontSize: 12 }}>
          密码由服务端 .env ADMIN_PASSWORD 配置
        </div>
      </Card>
    </div>
  )
}
