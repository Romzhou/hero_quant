import {render, screen} from "@testing-library/react"
import Chat from "../pages/Chat"
test("chat renders", () => { render(<Chat/>); expect(screen.getByText(/对话/)).toBeInTheDocument() })
