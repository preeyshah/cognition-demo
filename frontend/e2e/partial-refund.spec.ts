import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test('admin can approve a partial refund from the escalations queue', async ({ page }) => {
  await ssoLogin(page, 'priya.admin@acmepay.com')
  await page.getByRole('link', { name: 'Escalations' }).click()
  const approveBtn = page.locator('[data-testid^="approve-"]:not([disabled])').first()
  await expect(approveBtn).toBeVisible()
  const ref = (await approveBtn.getAttribute('data-testid'))!.replace('approve-', '')
  await approveBtn.click()
  const amount = page.getByTestId('decision-amount')
  const requested = parseFloat(await amount.inputValue())
  const partial = (Math.floor(requested * 50) / 100).toFixed(2)
  await amount.fill(partial)
  await page.getByTestId('decision-note').fill('Only half of the order was affected.')
  await page.getByTestId('decision-confirm').click()
  await expect(page.getByTestId('decision-confirm')).toHaveCount(0)

  await page.getByRole('link', { name: 'All refunds' }).click()
  await page.getByPlaceholder('Search reference, order, customer…').fill(ref)
  const row = page.locator(`[data-testid="row-${ref}"]`)
  await expect(row).toContainText(/approved/i)
  await expect(row).toContainText(/refunded \$/i)
  await row.click()
  await expect(page.getByTestId('partial-refund')).toBeVisible()
})
