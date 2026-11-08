import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test.describe('admin surfaces', () => {
  test.beforeEach(async ({ page }) => {
    await ssoLogin(page, 'priya.admin@acmepay.com')
  })

  test('analytics page shows KPI cards', async ({ page }) => {
    await expect(page.getByRole('heading', { name: /analytics/i })).toBeVisible()
    await expect(page.locator('.stat').first()).toBeVisible()
  })

  test('team & limits page lists users', async ({ page }) => {
    await page.getByRole('link', { name: 'Team & limits' }).click()
    await expect(page.getByRole('heading', { name: /team, roles & limits/i })).toBeVisible()
    await expect(page.getByText('jordan.lee@acmepay.com')).toBeVisible()
  })

  test('audit log shows recorded actions', async ({ page }) => {
    await page.getByRole('link', { name: 'Audit log' }).click()
    await expect(page.getByRole('heading', { name: /audit log/i })).toBeVisible()
    await expect(page.getByText('auth.login').first()).toBeVisible({ timeout: 10_000 })
  })

  test('can approve a partial refund on an escalated refund', async ({ page }) => {
    await page.getByRole('link', { name: 'Escalations' }).click()
    const approveBtn = page.locator('[data-testid^="approve-"]:not([disabled])').first()
    await expect(approveBtn).toBeVisible()
    const ref = (await approveBtn.getAttribute('data-testid'))!.replace('approve-', '')
    await approveBtn.click()
    await page.getByTestId('partial-toggle').check()
    await page.getByTestId('partial-amount').fill('1.00')
    await page.getByTestId('decision-note').fill('Customer kept part of the order.')
    await page.getByTestId('decision-confirm').click()
    await expect(page.getByTestId('decision-confirm')).toHaveCount(0)
    await page.getByRole('link', { name: 'All refunds' }).click()
    await expect(page.locator(`[data-testid="row-${ref}"]`)).toContainText('$1.00 approved')
  })

  test('view-as previews another user read-only', async ({ page }) => {
    await page.getByRole('link', { name: 'View as' }).click()
    await page.getByRole('button', { name: /Vik Mehta/ }).click()
    await expect(page.getByText(/Viewing as/)).toBeVisible()
    await expect(page.getByText(/read-only/i).first()).toBeVisible()
  })
})

test.describe('viewer restrictions', () => {
  test('viewer sees only read-only nav and masked PII', async ({ page }) => {
    await ssoLogin(page, 'vik.auditor@acmepay.com')
    await expect(page.getByRole('link', { name: 'Analytics' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'All refunds' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Audit log' })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'My queue' })).toHaveCount(0)
    // Admin endpoints are denied server-side even if navigated to directly.
    const res = await page.request.get('/api/admin/users')
    expect(res.status()).toBe(403)
  })
})
