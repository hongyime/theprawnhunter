'use client'

import type { CSSProperties, ReactNode } from 'react'

interface NeoCardProps {
  children: ReactNode
  className?: string
  style?: CSSProperties
}

export default function NeoCard({ children, className, style }: NeoCardProps) {
  return (
    <div
      className={className}
      style={{
        border: '2px solid var(--neo-border)',
        boxShadow: '4px 4px 0 var(--neo-shadow)',
        borderRadius: 0,
        background: 'var(--neo-bg)',
        ...style,
      }}
    >
      {children}
    </div>
  )
}
