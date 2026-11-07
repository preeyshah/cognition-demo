import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test.describe('compliance center', () => {
  test('admin sees live control results with evidence and can hand a failure to Devin', async ({ page }) => {
    await ssoLogin(page, 'priya.admin@acmepay.com')
    await page.getByRole('link', { name: 'Compliance' }).click()
    await expect(page.getByRole('heading', { name: 'Compliance Center' })).toBeVisible()
    await expect(page.locator('.stat').filter({ hasText: 'PCI DSS 4.0' })).toBeVisible()
    const timeout = page.getByTestId('control-IA-02')
    await expect(timeout).toContainText('fail')
    await timeout.click()
    await expect(page.locator('.evidence')).toContainText('access_token_minutes')
    await page.getByRole('button', { name: 'Fix with Devin' }).click()
    await expect(page.getByText(/Ticket #\d+/)).toBeVisible()
  })

  test('auditor (viewer) gets a read-only compliance view', async ({ page }) => {
    await ssoLogin(page, 'vik.auditor@acmepay.com')
    await page.getByRole('link', { name: 'Compliance' }).click()
    await expect(page.getByText(/Auditor view: read-only/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Access review' })).toHaveCount(0)
    await page.getByTestId('control-AC-02').click()
    await expect(page.getByRole('button', { name: 'Fix with Devin' })).toHaveCount(0)
  })
})
