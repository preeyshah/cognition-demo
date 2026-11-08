import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test.describe('partial refunds', () => {
  test('admin approves a partial refund; invalid amounts are blocked', async ({ page }) => {
    await ssoLogin(page, 'priya.admin@acmepay.com')
    await page.getByRole('link', { name: 'Escalations' }).click()
    const approveBtn = page.locator('[data-testid^="approve-"]:not([disabled])').first()
    await expect(approveBtn).toBeVisible()
    const ref = (await approveBtn.getAttribute('data-testid'))!.replace('approve-', '')
    await approveBtn.click()

    const amount = page.getByTestId('decision-amount')
    const confirm = page.getByTestId('decision-confirm')
    const requestedCents = Math.round(parseFloat(await amount.inputValue()) * 100)
    expect(requestedCents).toBeGreaterThan(1)

    for (const bad of ['abc', '0', '-5', String((requestedCents + 1) / 100)]) {
      await amount.fill(bad)
      await expect(page.getByTestId('decision-amount-error')).toBeVisible()
      await expect(confirm).toBeDisabled()
    }

    const partialCents = Math.floor(requestedCents / 2)
    await amount.fill((partialCents / 100).toFixed(2))
    await expect(page.getByTestId('decision-amount-error')).toHaveCount(0)
    const label = (await confirm.textContent())!.replace('Approve ', '')
    await page.getByTestId('decision-note').fill('Only the duplicate line item.')
    await confirm.click()
    await expect(confirm).toHaveCount(0)

    await page.getByRole('link', { name: 'Payouts' }).click()
    await expect(page.getByTestId(`payout-${ref}`)).toContainText(label)
  })

  test('reviewers get no partial amount field', async ({ page }) => {
    await ssoLogin(page, 'jordan.lee@acmepay.com')
    await page.locator('[data-testid^="approve-"]:not([disabled])').first().click()
    await expect(page.getByTestId('decision-confirm')).toBeVisible()
    await expect(page.getByTestId('decision-amount')).toHaveCount(0)
  })
})
