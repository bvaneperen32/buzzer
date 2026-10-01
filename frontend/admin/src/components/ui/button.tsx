import { cn } from '../../lib/utils';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'outline' | 'ghost' | 'destructive';
  size?: 'sm' | 'md' | 'lg';
}

export function Button({ className, variant = 'default', size = 'md', ...props }: ButtonProps) {
  return (
    <button
      className={cn(
        'inline-flex items-center justify-center rounded-lg font-semibold transition-colors',
        'disabled:opacity-50 disabled:pointer-events-none',
        variant === 'default' && 'bg-accent text-on-accent hover:bg-accent-hover',
        variant === 'outline' && 'border border-input text-primary hover:bg-hover',
        variant === 'ghost' && 'text-secondary hover:bg-hover hover:text-primary',
        variant === 'destructive' && 'bg-danger text-on-danger hover:bg-danger-hover',
        size === 'sm' && 'px-3 py-1.5 text-sm',
        size === 'md' && 'px-4 py-2 text-base',
        size === 'lg' && 'px-6 py-3 text-lg',
        className
      )}
      {...props}
    />
  );
}
