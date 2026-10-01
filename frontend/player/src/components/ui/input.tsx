import { cn } from '../../lib/utils';

type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

export function Input({ className, ...props }: InputProps) {
  return (
    <input
      className={cn(
        'w-full rounded-xl border border-input bg-raised px-4 py-3',
        'text-primary placeholder:text-secondary text-base',
        'focus-visible:border-accent',
        className
      )}
      {...props}
    />
  );
}
