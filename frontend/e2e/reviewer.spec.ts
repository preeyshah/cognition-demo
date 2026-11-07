import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test.describe('reviewer workflow', () => {
  test.beforeEach(async ({ page }) => {
    await ssoLogin(page, 'jordan.lee@acmepay.com')
  })

  test('queue lists assigned refunds and opens the detail drawer', async ({ page }) => {
    const firstRow = page.locator('[data-testid^="row-"]').first()
    await expect(firstRow).toBeVisible()
    await firstRow.click()
    // Drawer shows details + timeline
    await expect(page.getByText(/timeline|events/i).first()).toBeVisible()
  })

  test('can approve an eligible refund', async ({ page }) => {
    const approveBtn = page.locator('[data-testid^="approve-"]:not([disabled])').first()
    await expect(approveBtn).toBeVisible()
    const ref = (await approveBtn.getAttribute('data-testid'))!.replace('approve-', '')
    await approveBtn.click()
    await expect(page.getByTestId('decision-confirm')).toBeVisible()
    await page.getByTestId('decision-note').fill('Verified against the transaction ledger.')
    await page.getByTestId('decision-confirm').click()
    // Modal closes and the decided refund leaves the pending-only queue.
    await expect(page.getByTestId('decision-confirm')).toHaveCount(0)
    await expect(page.locator(`[data-testid="row-${ref}"]`)).toHaveCount(0)
    // …and shows up under My history as approved.
    await page.getByRole('link', { name: 'My history' }).click()
    await expect(page.locator(`[data-testid="row-${ref}"]`)).toContainText(/approved/i)
  })

  test('reject requires a note', async ({ page }) => {
    const rejectBtn = page.locator('[data-testid^="reject-"]').first()
    await rejectBtn.click()
    // Confirm with an empty/short note is blocked client-side by policy message or server 403.
    await page.getByTestId('decision-confirm').click()
    await expect(page.getByTestId('decision-confirm')).toBeVisible() // modal stays open with error
  })

  test('customer history loads from the provider', async ({ page }) => {
    const historyBtn = page.locator('[data-testid^="history-"]').first()
    test.skip((await historyBtn.count()) === 0, 'no history-eligible refund in queue')
    await historyBtn.click()
    await expect(page.getByText(/KYC|risk flags|transactions/i).first()).toBeVisible({ timeout: 10_000 })
  })
})
