import {render, screen} from "@testing-library/react"
import Research from "../pages/Research"
test("research renders tearsheet", ()=>{render(<Research/>); expect(screen.getByText(/本月收益热力|累积收益/)).toBeInTheDocument()})
