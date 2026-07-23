import { Button, Card, Form, Input, message } from 'antd'
import { LockOutlined, TeamOutlined } from '@ant-design/icons'

import { loginTenant } from '../tenantApi.js'

export default function TenantLogin({ onLogin }) {
  const [form] = Form.useForm()
  const [messageApi, messageContextHolder] = message.useMessage()

  const submit = async ({ tenant_id: tenantId, password }) => {
    try {
      await loginTenant(tenantId, password)
      onLogin()
      messageApi.success('登录成功')
    } catch (error) {
      messageApi.error(error.response?.status === 401 ? '租户 ID 或密码错误' : '登录失败，请稍后重试')
    } finally {
      form.setFieldValue('password', '')
    }
  }

  return (
    <>
      {messageContextHolder}
      <main className="tenant-login">
        <Card title="租户控制台登录" className="tenant-login__card">
          <Form form={form} layout="vertical" onFinish={submit} autoComplete="off">
            <Form.Item
              name="tenant_id"
              label="租户 ID"
              rules={[{ required: true, message: '请输入租户 ID' }]}
            >
              <Input prefix={<TeamOutlined />} autoComplete="username" size="large" />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                autoComplete="current-password"
                size="large"
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" block size="large">登录</Button>
          </Form>
        </Card>
      </main>
    </>
  )
}
