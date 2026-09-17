'use client'

import { useState } from 'react'
import type { CSSProperties, MouseEventHandler, ReactNode } from 'react'

interface NeoButtonProps {
  children: ReactNode
  onClick?: MouseEventHandler<HTMLButtonElement>
  type?: 'button' | 'submit' | 'reset'
  disabled?: boolean
  className?: string
  style?: CSSProperties
}

export default function NeoButton({
  children,
  onClick,
  type = 'button',
  disabled,
  className,
  style,
}: NeoButtonProps) {
  const [pressed, setPressed] = useState(false)

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={className}
      style={{
        border: '2px solid var(--neo-border)',
        boxShadow: pressed ? '0 0 0' : '3px 3px 0 var(--neo-shadow)',
        borderRadius: 0,
        background: 'var(--neo-bg)',
        padding: '8px 16px',
        fontFamily: 'inherit',
        fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transform: pressed ? 'translate(3px, 3px)' : 'none',
        transition: 'box-shadow 0.1s, transform 0.1s',
        ...style,
      }}
      onMouseDown={() => { if (!disabled) setPressed(true) }}
      onMouseUp={() => setPressed(false)}
      onMouseLeave={() => setPressed(false)}
    >
      {children}
    </button>
  )
}
