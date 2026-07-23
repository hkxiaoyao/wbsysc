import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, List, Modal, Space, Tag, Typography } from 'antd'
import { CopyOutlined, EyeOutlined, KeyOutlined } from '@ant-design/icons'

import {
  apiClientEndpoint,
  closeServiceTokenState,
  copyTokenWithLifecycle,
  createTokenRevealSequence,
  failedServiceTokenState,
  safeServiceError,
  canonicalTokenTimestamp,
  serviceTokenIssuePayload,
  serviceResourceEndpoint,
  tokenCanCopy,
  tokenCanReveal,
  tokenCanRevoke,
  tokenLifecycleStatus,
} from './servicesView.js'

const { Text } = Typography

export default function ServiceTokenModal({
  open,
  service,
  tokens = [],
  scope = 'admin',
  tenantId = '',
  apiClient,
  onClose,
  onTokensChange,
}) {
  const serviceId = service?.service_id || ''
  const [label, setLabel] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [state, setState] = useState(closeServiceTokenState)
  const [mutationBusy, setMutationBusy] = useState('')
  const [modal, modalContextHolder] = Modal.useModal()
  const sequence = useRef(createTokenRevealSequence())
  const controller = useRef(null)
  const visible = useRef(false)
  const activeService = useRef('')

  const clearSensitiveState = () => {
    sequence.current.invalidate()
    controller.current?.abort()
    controller.current = null
    setState(closeServiceTokenState())
  }

  useEffect(() => {
    visible.current = Boolean(open)
    activeService.current = serviceId
    clearSensitiveState()
    setLabel('')
    setExpiresAt('')
    setMutationBusy('')
    if (open && serviceId) {
      setState({ ...closeServiceTokenState(), open: true, serviceId })
    }
  }, [open, serviceId])

  useEffect(() => () => {
    visible.current = false
    activeService.current = ''
    sequence.current.invalidate()
    controller.current?.abort()
    setExpiresAt('')
  }, [])

  const endpoint = suffix => apiClientEndpoint(
    apiClient,
    serviceResourceEndpoint(scope, tenantId, serviceId, suffix),
  )

  const close = () => {
    visible.current = false
    clearSensitiveState()
    setLabel('')
    setExpiresAt('')
    setMutationBusy('')
    onClose?.()
  }

  const copyRawToken = async (rawToken, tokenId) => {
    const outcome = await copyTokenWithLifecycle({
      sequence: sequence.current,
      serviceId,
      tokenId,
      rawToken,
      writeText: value => {
        if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
        return navigator.clipboard.writeText(value)
      },
      isActive: () => visible.current && activeService.current === serviceId,
      onCurrentFailure: () => {
        sequence.current.invalidate()
        setState(failedServiceTokenState(serviceId, 'Token 复制失败，请重试'))
      },
    })
    return outcome === 'copied'
  }

  const reveal = async (token, copyAfterReveal = false) => {
    if (!tokenCanReveal(token) || !serviceId) return
    const tokenId = token.token_id
    if (state.serviceId === serviceId && state.tokenId === tokenId && tokenCanCopy({ raw_value: state.rawToken })) {
      if (copyAfterReveal) await copyRawToken(state.rawToken, tokenId)
      return
    }

    controller.current?.abort()
    const requestController = new AbortController()
    controller.current = requestController
    const ticket = sequence.current.begin(serviceId, tokenId)
    setState({
      open: true,
      serviceId,
      tokenId,
      rawToken: '',
      revealBusy: true,
      error: '',
    })
    try {
      const response = await apiClient.post(endpoint(`tokens/${tokenId}/reveal`), undefined, {
        signal: requestController.signal,
      })
      if (
        requestController.signal.aborted
        || !visible.current
        || activeService.current !== serviceId
        || !sequence.current.isCurrent(ticket, serviceId, tokenId)
      ) return
      const rawToken = typeof response.data?.token === 'string' ? response.data.token : ''
      if (!rawToken) throw new Error('empty token response')
      setState({ open: true, serviceId, tokenId, rawToken, revealBusy: false, error: '' })
      if (copyAfterReveal) await copyRawToken(rawToken, tokenId)
    } catch (error) {
      if (requestController.signal.aborted || !sequence.current.isCurrent(ticket, serviceId, tokenId)) return
      sequence.current.invalidate()
      setState(failedServiceTokenState(
        serviceId,
        safeServiceError(error, 'Token 查看失败，请重试'),
      ))
    }
  }

  const issue = async () => {
    if (!serviceId) return
    controller.current?.abort()
    const requestController = new AbortController()
    controller.current = requestController
    const requestKey = '__issue__'
    const ticket = sequence.current.begin(serviceId, requestKey)
    setMutationBusy('issue')
    setState({ open: true, serviceId, tokenId: '', rawToken: '', revealBusy: false, error: '' })
    try {
      const response = await apiClient.post(endpoint('tokens'), serviceTokenIssuePayload(label, expiresAt), {
        signal: requestController.signal,
      })
      if (
        requestController.signal.aborted
        || !visible.current
        || activeService.current !== serviceId
        || !sequence.current.isCurrent(ticket, serviceId, requestKey)
      ) return
      const rawToken = typeof response.data?.token === 'string' ? response.data.token : ''
      if (!rawToken) throw new Error('empty token response')
      setLabel('')
      setExpiresAt('')
      setState({
        open: true,
        serviceId,
        tokenId: response.data?.token_id || '',
        rawToken,
        revealBusy: false,
        error: '',
      })
      await onTokensChange?.()
    } catch (error) {
      if (requestController.signal.aborted || !sequence.current.isCurrent(ticket, serviceId, requestKey)) return
      sequence.current.invalidate()
      setState(failedServiceTokenState(
        serviceId,
        safeServiceError(error, 'Token 签发失败，请重试'),
      ))
    } finally {
      if (!requestController.signal.aborted && visible.current && activeService.current === serviceId) setMutationBusy('')
    }
  }

  const revoke = token => modal.confirm({
    title: '撤销这个服务 Token？',
    content: `仅凭前缀 ${token.prefix || '—'} 识别；撤销后使用它的客户端会立即失效。`,
    okText: '撤销 Token',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: async () => {
      clearSensitiveState()
      setMutationBusy(token.token_id)
      try {
        await apiClient.delete(endpoint(`tokens/${token.token_id}`))
        if (visible.current && activeService.current === serviceId) await onTokensChange?.()
      } catch (error) {
        if (visible.current && activeService.current === serviceId) {
          setState({
            ...closeServiceTokenState(),
            open: true,
            serviceId,
            error: safeServiceError(error, 'Token 撤销失败，请重试'),
          })
          throw error
        }
      } finally {
        if (visible.current && activeService.current === serviceId) setMutationBusy('')
      }
    },
  })

  const copyVisible = async () => {
    if (tokenCanCopy({ raw_value: state.rawToken })) await copyRawToken(state.rawToken, state.tokenId)
  }

  return (
    <Modal
      title={service ? `${service.display_name} · 服务 Token` : '服务 Token'}
      open={Boolean(open && serviceId)}
      onCancel={close}
      footer={<Button onClick={close}>关闭并清除 Token 原文</Button>}
      destroyOnClose
      maskClosable={false}
      width={760}
    >
      {modalContextHolder}
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert type="warning" showIcon message="Token 前缀仅用于识别，不能复制为凭据" description="只有签发结果或你主动点击“查看/复制”后，原文才会短暂保存在当前弹窗内；关闭或切换请求会立即清除。" />
        <Space.Compact style={{ width: '100%' }}>
          <Input aria-label="服务 Token 用途标签" value={label} maxLength={128} placeholder="用途标签（可选）" onChange={event => setLabel(event.target.value)} />
          <Input aria-label="服务 Token 过期时间" type="datetime-local" step="1" value={expiresAt} onChange={event => setExpiresAt(event.target.value)} />
          <Button type="primary" icon={<KeyOutlined />} loading={mutationBusy === 'issue'} disabled={Boolean(mutationBusy)} onClick={issue}>签发 Token</Button>
        </Space.Compact>
        {state.error ? <Alert type="error" showIcon message={state.error} /> : null}
        {state.rawToken ? (
          <Alert
            type="success"
            showIcon
            message="Token 原文仅在当前弹窗短暂显示"
            description={<Space direction="vertical" style={{ width: '100%' }}><Input.TextArea aria-label="服务 Token 原文" readOnly autoSize value={state.rawToken} /><Button icon={<CopyOutlined />} onClick={copyVisible}>复制当前 Token</Button></Space>}
          />
        ) : null}
        <List
          bordered
          dataSource={tokens}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无服务 Token" /> }}
          renderItem={token => {
            const revealable = tokenCanReveal(token)
            const revocable = tokenCanRevoke(token)
            const lifecycle = tokenLifecycleStatus(token)
            const busy = state.revealBusy && state.tokenId === token.token_id
            const actions = []
            if (revealable) {
              actions.push(
                <Button key="reveal" type="link" icon={<EyeOutlined />} loading={busy} disabled={Boolean(mutationBusy)} onClick={() => reveal(token, false)}>查看</Button>,
                <Button key="copy" type="link" icon={<CopyOutlined />} loading={busy} disabled={Boolean(mutationBusy)} onClick={() => reveal(token, true)}>复制</Button>,
              )
            }
            if (revocable) {
              actions.push(<Button key="revoke" danger type="link" loading={mutationBusy === token.token_id} disabled={Boolean(mutationBusy)} onClick={() => revoke(token)}>撤销</Button>)
            }
            return (
              <List.Item
                actions={actions}
              >
                <List.Item.Meta
                  title={(
                    <Space>
                      <Text code>{token.prefix || '无前缀'}</Text>
                      <Text>{token.label || '未命名'}</Text>
                      {lifecycle === 'active' ? <Tag color="green">有效</Tag> : null}
                      {lifecycle === 'expired' ? <Tag color="orange">已过期</Tag> : null}
                      {lifecycle === 'revoked' ? <Tag>已撤销</Tag> : null}
                    </Space>
                  )}
                  description={(
                    <Space direction="vertical" size={0}>
                      <Text type="secondary">签发时间：{canonicalTokenTimestamp(token.created_at) || '未知'}</Text>
                      <Text type="secondary">过期时间：{token.expires_at == null ? '永不过期' : (canonicalTokenTimestamp(token.expires_at) || '无效')}</Text>
                      <Text type="secondary">最后使用：{token.last_used_at == null ? '从未使用' : (canonicalTokenTimestamp(token.last_used_at) || '无效')}</Text>
                    </Space>
                  )}
                />
              </List.Item>
            )
          }}
        />
      </Space>
    </Modal>
  )
}
