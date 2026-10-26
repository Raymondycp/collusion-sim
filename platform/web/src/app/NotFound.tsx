import { Link } from 'react-router'

export function NotFound() {
  return (
    <div className="py-16 text-center">
      <p className="text-lg font-medium">This page does not exist.</p>
      <Link to="/runs" className="text-sm underline underline-offset-4">
        Go to the run list
      </Link>
    </div>
  )
}
